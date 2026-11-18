"""Bounds, the kill switch, and the per-session counters.

Three layers, in increasing order of how much they actually protect you:

1. Per-call bounds (max bid, max budget, max % change). These catch a typo --
   a bid of "120.00" where "1.20" was meant.

2. The kill switch, `.audit/DISABLE_WRITES`. A FILE, not an environment
   variable, because a long-lived stdio server never sees an env var exported
   after it started, whereas `touch .audit/DISABLE_WRITES` from another terminal
   halts every apply_* within one tool call.

3. Per-session counters, and a per-DAY total read back from the ledger. These
   are the ones that matter. A per-call limit is no defence at all against a
   loop making 400 individually-legal changes, and that is the failure mode an
   agent has and a human does not. The session counter alone was not enough
   either: it lives in memory, so every restart -- and every second MCP process,
   one per Claude session -- started with a full budget. The daily total is
   computed from the `changes` table, so every process on this machine shares it.

Defaults live here as code constants and are overridden by guardrails.toml,
which is committed, so changing a limit shows up as a diff rather than as an
export someone did once.
"""

from __future__ import annotations

import datetime as dt
import os
import threading
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from apple_ads_mcp.config import REPO_ROOT, load_config

GUARDRAILS_FILE = REPO_ROOT / "guardrails.toml"
LOCAL_GUARDRAILS_FILE = REPO_ROOT / "guardrails.local.toml"
AUDIT_DIR = REPO_ROOT / ".audit"
KILL_SWITCH = AUDIT_DIR / "DISABLE_WRITES"

_DEFAULTS: dict[str, object] = {
    "max_bid": "5.00",
    "max_bid_increase_pct": 50.0,
    "max_daily_budget": "150.00",
    "max_budget_change_pct": 25.0,
    "max_entities_per_call": 25,
    "max_projected_delta_per_day": "50.00",
    "max_applies_per_session": 20,
    "max_projected_session_delta": "150.00",
    "max_cpa_goal": "20.00",
    "max_cpa_goal_change_pct": 50.0,
    "max_applies_per_day": 40,
    "max_projected_delta_per_day_total": "200.00",
}
_SCOPE_DEFAULTS: dict[str, object] = {
    "allowed_campaign_ids": [],
    "allowed_currencies": ["CAD"],
}


class GuardrailConfigError(RuntimeError):
    """guardrails.toml is malformed. Fail at startup, never silently ignore."""


@dataclass(frozen=True)
class Limits:
    max_bid: Decimal
    max_bid_increase_pct: float
    max_daily_budget: Decimal
    max_budget_change_pct: float
    max_entities_per_call: int
    max_projected_delta_per_day: Decimal
    max_applies_per_session: int
    max_projected_session_delta: Decimal
    max_applies_per_day: int = 40
    max_cpa_goal: Decimal = Decimal("20.00")
    max_cpa_goal_change_pct: float = 50.0
    max_projected_delta_per_day_total: Decimal = Decimal("200.00")
    allowed_campaign_ids: frozenset[int] = field(default_factory=frozenset)
    allowed_currencies: frozenset[str] = field(default_factory=frozenset)
    source: str = "defaults"


def _coerce(key: str, value: object) -> object:
    if key in (
        "max_bid",
        "max_daily_budget",
        "max_projected_delta_per_day",
        "max_projected_session_delta",
        "max_projected_delta_per_day_total",
        "max_cpa_goal",
    ):
        return Decimal(str(value))
    if key in ("max_bid_increase_pct", "max_budget_change_pct", "max_cpa_goal_change_pct"):
        return float(value)
    if key in ("max_entities_per_call", "max_applies_per_session", "max_applies_per_day"):
        return int(value)
    return value


def _read_toml(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise GuardrailConfigError(f"{path.name} is not valid TOML: {exc}") from exc


def load_limits() -> Limits:
    """Code defaults, then guardrails.toml, then guardrails.local.toml, then .env scope."""
    values = dict(_DEFAULTS)
    scope = dict(_SCOPE_DEFAULTS)
    sources = ["defaults"]

    for path in (GUARDRAILS_FILE, LOCAL_GUARDRAILS_FILE):
        data = _read_toml(path)
        if not data:
            continue
        sources.append(path.name)
        for key, value in (data.get("limits") or {}).items():
            if key not in _DEFAULTS:
                raise GuardrailConfigError(
                    f"{path.name}: unknown key [limits].{key}. Known keys: "
                    f"{', '.join(sorted(_DEFAULTS))}"
                )
            values[key] = value
        for key, value in (data.get("scope") or {}).items():
            if key not in _SCOPE_DEFAULTS:
                raise GuardrailConfigError(
                    f"{path.name}: unknown key [scope].{key}. Known keys: "
                    f"{', '.join(sorted(_SCOPE_DEFAULTS))}"
                )
            scope[key] = value

    # Campaign ids are real identifiers, so .env is where they belong. The TOML
    # list wins when both are set -- an explicit committed pin should not be
    # quietly widened by a stale env var.
    campaign_ids = [int(c) for c in (scope.get("allowed_campaign_ids") or [])]
    if not campaign_ids:
        raw = (load_config().get("APPLE_ADS_ALLOWED_CAMPAIGN_IDS") or "").strip()
        if raw:
            campaign_ids = [int(part) for part in raw.replace(" ", "").split(",") if part]
            sources.append(".env")

    try:
        coerced = {key: _coerce(key, value) for key, value in values.items()}
    except (ValueError, ArithmeticError) as exc:
        raise GuardrailConfigError(f"guardrails: {exc}") from exc

    return Limits(
        **coerced,  # type: ignore[arg-type]
        allowed_campaign_ids=frozenset(campaign_ids),
        allowed_currencies=frozenset(
            str(c).upper() for c in (scope.get("allowed_currencies") or [])
        ),
        source=" + ".join(sources),
    )


def limit_warnings(limits: Limits) -> list[str]:
    """Combinations of limits that make some legitimate change impossible.

    Checked at startup WITHOUT calling Apple (a server must start offline), so
    it compares against max_daily_budget -- the largest budget this server could
    have set -- rather than against each serving campaign. A campaign whose
    budget exceeds the daily total can never be re-enabled from here, and its
    campaign_status preview says so with both figures.
    """
    warnings = []
    if limits.max_projected_delta_per_day_total < limits.max_daily_budget:
        warnings.append(
            f"max_projected_delta_per_day_total ({limits.max_projected_delta_per_day_total}) "
            f"is below max_daily_budget ({limits.max_daily_budget}): re-enabling a "
            f"campaign projects its whole daily budget against the daily total, so a "
            f"campaign budgeted above the total could never be re-enabled from here."
        )
    if limits.max_projected_session_delta < limits.max_daily_budget:
        warnings.append(
            f"max_projected_session_delta ({limits.max_projected_session_delta}) is "
            f"below max_daily_budget ({limits.max_daily_budget}): the same applies to "
            f"the session total."
        )
    return warnings


# --- read-only mode ----------------------------------------------------------

READ_ONLY_ENV = "APPLE_ADS_READ_ONLY"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def read_only_mode() -> bool:
    """True when this process must publish no `apply_*` tool at all.

    A FOURTH layer, and the only one that works no matter which client is
    driving. Claude Code can allowlist tools by name, so there "never approve an
    apply" is something the client can express. Claude Desktop, Cursor and Codex
    have no equivalent -- their MCP config says which servers to run, not which
    tools may be called -- so for them the only honest way to offer a read-only
    install is for the server to publish no write tools in the first place. An
    unregistered tool is unreachable, exactly as an unwritten one is.

    Read from the PROCESS environment rather than from .env, and that is the
    point: every client's MCP config has its own `env` block, so one checkout
    can be read-only for one client and writable for another without two copies
    of the repo or two sets of credentials.

    Not the same thing as the kill switch. `DISABLE_WRITES` is a file, it stops
    an already-published apply mid-session, and it is account-wide. This is set
    once at startup, per client, and the tools simply never appear.
    """
    return os.environ.get(READ_ONLY_ENV, "").strip().lower() in _TRUTHY


# --- kill switch -------------------------------------------------------------


def writes_disabled() -> str | None:
    """The kill switch reason, or None. Checked inside every apply_*, not cached."""
    if not KILL_SWITCH.exists():
        return None
    try:
        note = KILL_SWITCH.read_text().strip()
    except OSError:
        note = ""
    return note or f"{KILL_SWITCH.name} exists"


def disable_writes(reason: str = "") -> Path:
    # `.audit/` outlived the ledger that used to live in it: the change record
    # moved to logs/apple-ads.db, but the kill switch is still a FILE here, on
    # purpose -- see the note above KILL_SWITCH. Nothing else creates the
    # directory now, so this is the only caller that has to.
    ensure_audit_dir()
    KILL_SWITCH.write_text(reason or "writes disabled by hand\n")
    return KILL_SWITCH


# --- session counters --------------------------------------------------------


class SessionCounters:
    """What this server process has already applied. Reset only by a restart.

    Deliberately NOT persisted. The question these answer is "has this agent run
    away during this conversation", and a counter that survives a restart would
    make an ordinary new session start out already half-spent.
    """

    def __init__(self, limits: Limits) -> None:
        self._limits = limits
        self._lock = threading.Lock()
        self.applies = 0
        self.projected_delta = Decimal("0")

    @property
    def limits(self) -> Limits:
        return self._limits

    def would_exceed(self, projected_delta: Decimal, cost: int = 1) -> str | None:
        """The reason this apply would breach a session budget, or None.

        `cost` is how many applies the change counts as: 1, or 3 for a creation.
        """
        with self._lock:
            if self.applies + cost > self._limits.max_applies_per_session:
                return (
                    f"session limit reached: {self.applies} applies already made, "
                    f"max_applies_per_session is {self._limits.max_applies_per_session}. "
                    f"Restart the server to reset, after checking what those changes were "
                    f"with list_my_changes."
                )
            total = self.projected_delta + max(projected_delta, Decimal("0"))
            if total > self._limits.max_projected_session_delta:
                return (
                    f"session spend limit reached: this change would take the projected "
                    f"daily-spend increase for this session to {total}, over "
                    f"max_projected_session_delta of "
                    f"{self._limits.max_projected_session_delta}."
                )
        return None

    def record(self, projected_delta: Decimal, cost: int = 1) -> None:
        with self._lock:
            self.applies += cost
            self.projected_delta += max(projected_delta, Decimal("0"))

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "applies_this_session": self.applies,
                "max_applies_per_session": self._limits.max_applies_per_session,
                "projected_session_delta": str(self.projected_delta),
                "max_projected_session_delta": str(self._limits.max_projected_session_delta),
            }


# --- the daily total ----------------------------------------------------------


def local_midnight_utc(now: dt.datetime | None = None) -> dt.datetime:
    """Today's LOCAL midnight, as an aware UTC datetime.

    "Today" is the operator's calendar day, not UTC's: in the evening in North
    America the UTC date has already rolled over, and a cap counted in UTC would
    reset in the middle of their working day. The ledger's `ts` column is UTC
    ISO (`logdb._now()`), so the boundary is computed in local time and then
    converted. `.astimezone()` applies the local zone's rules for that instant,
    so a DST change between midnight and now is accounted for.
    """
    local_now = (now or dt.datetime.now()).astimezone()
    midnight = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(dt.timezone.utc)


class DailyUsage:
    """Today's applies and projected spend, read back from the ledger.

    No table of its own and no in-memory state: every answer comes from the
    `changes` rows since local midnight, so two server processes -- or one
    restarted ten times today -- see the same numbers. Only rows whose outcome
    is applied or partial count, and unknown ones (a write that timed out may
    have landed); a write Apple refused spent nothing.

    A batch counts once (its rows share a batch_id) and costs what its kind
    costs (3 for a creation). The projected spend is the sum of each row's own
    positive delta: a reduction refunds nothing, exactly as in SessionCounters.
    """

    def __init__(self, limits: Limits, now: dt.datetime | None = None) -> None:
        self._limits = limits
        self._now = now

    def usage(self) -> tuple[int, Decimal]:
        from apple_ads_mcp import logdb

        groups: dict[str, int] = {}
        delta = Decimal("0")
        for row in logdb.successful_changes_since(local_midnight_utc(self._now)):
            key = str(row.get("batch_id") or row["entry_id"])
            try:
                groups[key] = int(row.get("cost") or 1)
            except (TypeError, ValueError):
                groups[key] = 1
            try:
                value = Decimal(str(row.get("projected_daily_spend_delta") or "0"))
            except (InvalidOperation, ValueError):
                value = Decimal("0")
            delta += max(value, Decimal("0"))
        return sum(groups.values()), delta

    def check(self, projected: Decimal, used: Decimal | None = None) -> Check:
        """The daily-total line for a preview. Names both figures, always.

        "Over the ceiling" alone does not tell a human whether the change is too
        big or the day is already spent.
        """
        if used is None:
            used = self.usage()[1]
        total = self._limits.max_projected_delta_per_day_total
        remaining = max(Decimal("0"), total - used)
        wanted = max(projected, Decimal("0"))
        passed = wanted <= remaining
        return Check(
            "projected_daily_total",
            passed,
            f"uses {wanted} of today's remaining {remaining} "
            f"({used} already used of max_projected_delta_per_day_total {total})"
            + ("" if passed else " -- too much for today"),
        )

    def would_exceed(self, projected: Decimal, cost: int = 1) -> str | None:
        applies, used = self.usage()
        if applies + cost > self._limits.max_applies_per_day:
            return (
                f"daily apply limit reached: {applies} applies already made today "
                f"(local time, across every server process and the CLI), "
                f"max_applies_per_day is {self._limits.max_applies_per_day}. It resets "
                f"at local midnight. Stop and tell the human; the cap is set in "
                f"guardrails.toml."
            )
        check = self.check(projected, used)
        if not check.passed:
            return (
                f"daily spend limit reached: this change {check.detail}. It resets at "
                f"local midnight."
            )
        return None

    def snapshot(self) -> dict[str, str]:
        applies, used = self.usage()
        return {
            "day_started_at_utc": local_midnight_utc(self._now).isoformat(timespec="seconds"),
            "applies_today": str(applies),
            "max_applies_per_day": str(self._limits.max_applies_per_day),
            "projected_delta_today": str(used),
            "max_projected_delta_per_day_total": str(
                self._limits.max_projected_delta_per_day_total
            ),
        }


# --- per-change checks -------------------------------------------------------


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    #: A check that does not apply to this change, shown so the reader can see
    #: it was considered rather than forgotten. Never blocks.
    skipped: bool = False

    def render(self) -> str:
        label = "SKIP" if self.skipped else ("OK  " if self.passed else "FAIL")
        return f"{label} {self.name}: {self.detail}"


def check_campaign_in_scope(limits: Limits, campaign_id: int | None) -> Check:
    if not limits.allowed_campaign_ids:
        return Check("campaign_scope", True, "no ALLOWED_CAMPAIGN_IDS pin; every campaign is in scope")
    if campaign_id is None:
        return Check("campaign_scope", False, "could not determine the campaign for this entity")
    allowed = campaign_id in limits.allowed_campaign_ids
    return Check(
        "campaign_scope",
        allowed,
        f"campaign {campaign_id} is {'in' if allowed else 'NOT in'} the pinned set "
        f"({len(limits.allowed_campaign_ids)} campaign(s))",
    )


def check_currency(limits: Limits, currency: str | None) -> Check:
    if not limits.allowed_currencies:
        return Check("currency", True, "no currency allowlist configured")
    if currency is None:
        return Check("currency", False, "the amount carries no currency")
    allowed = currency.upper() in limits.allowed_currencies
    return Check(
        "currency",
        allowed,
        f"{currency} is {'allowed' if allowed else 'NOT allowed'} "
        f"({', '.join(sorted(limits.allowed_currencies))})",
    )


def check_ceiling(name: str, value: Decimal, ceiling: Decimal, unit: str = "") -> Check:
    passed = value <= ceiling
    suffix = f" {unit}" if unit else ""
    return Check(
        name,
        passed,
        f"{value}{suffix} vs ceiling {ceiling}{suffix}"
        + ("" if passed else " -- over the limit"),
    )


def check_pct_increase(name: str, before: Decimal | None, after: Decimal, ceiling_pct: float) -> Check:
    if before is None or before == 0:
        return Check(name, True, f"no previous value to compare against (now {after})")
    pct = float((after - before) / before * 100)
    if pct <= 0:
        return Check(name, True, f"{pct:+.1f}% -- a decrease is never blocked by this check")
    passed = pct <= ceiling_pct
    return Check(
        name,
        passed,
        f"{pct:+.1f}% ({before} -> {after}) vs ceiling +{ceiling_pct:.0f}%"
        + ("" if passed else " -- over the limit"),
    )


def check_known_status(name: str, raw_status: str | None) -> Check:
    """Refuse to write to an entity whose current status we could not parse.

    The generated client maps an unrecognised enum to `unknown_default_open_api`
    rather than raising. On a read that is the right call. As the `before` state
    of a write it is not: there is no honest way to describe the change, and
    nothing coherent for revert_change to put back.
    """
    unknown = raw_status in (None, "unknown_default_open_api")
    return Check(
        name,
        not unknown,
        "current status did not deserialize to a known value "
        f"({raw_status!r}) -- refusing to write over a state we cannot describe"
        if unknown
        else f"current status is {raw_status}",
    )


# Verified live 2026-10-08: under an automated bid strategy, Apple accepts a
# keyword-bid PUT with HTTP 200 and leaves the bid unchanged. Nothing in the
# response says it was ignored, so without this check the agent reports a
# successful bid change that never happened. The same is true of the ad group's
# own default bid, which is why `what` exists rather than a second check.
MANUAL_BID_STRATEGIES = frozenset({"MANUAL_CPT", "MANUAL_CPM"})


def check_bid_settable(bid_strategy_type: str | None, what: str = "keyword bid") -> Check:
    """A bid only means anything under a MANUAL bid strategy.

    Applies to the ad group's own default bid exactly as it does to a keyword
    bid: under MAX_CONVERSIONS or MAX_ENGAGEMENTS Apple sets the bids, and one
    written by hand is accepted and then ignored.
    """
    if bid_strategy_type is None:
        return Check(
            "bid_is_settable",
            False,
            "could not read the ad group's bid strategy, so there is no way to tell "
            f"whether the {what} would be used at all",
        )
    settable = bid_strategy_type in MANUAL_BID_STRATEGIES
    return Check(
        "bid_is_settable",
        settable,
        f"ad group bid strategy is {bid_strategy_type}"
        + (
            ""
            if settable
            else f" -- Apple sets the bids. A write to the {what} is accepted with "
            "HTTP 200 and then IGNORED, so this would report success and change "
            "nothing. Change the ad group's bid strategy first, in the Apple Ads UI."
        ),
    )


def check_count(limits: Limits, count: int) -> Check:
    passed = count <= limits.max_entities_per_call
    return Check(
        "entities_per_call",
        passed,
        f"{count} entit{'y' if count == 1 else 'ies'} vs max_entities_per_call "
        f"{limits.max_entities_per_call}" + ("" if passed else " -- too many"),
    )


def check_projected_delta(limits: Limits, projected: Decimal, kind: str = "change") -> Check:
    """The PER-CHANGE spend bound. `kind` is "change" or "enable".

    An ENABLE -- resuming a paused campaign, ad group, keyword or ad, or pausing
    a negative keyword -- projects the whole spend it can resume, which on any
    real campaign is over this per-change limit: a 100/day campaign could never
    be re-enabled under a 50 limit. So an enable is exempt from this line (shown
    as SKIP, not OK, so nobody mistakes it for having passed) and counted in full
    against the DAILY total and the session total instead. Bid and budget
    increases are not enables and must pass both.
    """
    if kind == "enable":
        return Check(
            "projected_daily_delta",
            True,
            "enable: counted against the daily total",
            skipped=True,
        )
    passed = projected <= limits.max_projected_delta_per_day
    return Check(
        "projected_daily_delta",
        passed,
        f"{projected} vs max_projected_delta_per_day "
        f"{limits.max_projected_delta_per_day}" + ("" if passed else " -- too much"),
    )


def ensure_audit_dir() -> Path:
    AUDIT_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(AUDIT_DIR, 0o700)
    except OSError:
        pass
    return AUDIT_DIR
