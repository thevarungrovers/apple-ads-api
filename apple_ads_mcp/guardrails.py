"""Bounds, the kill switch, and the per-session counters.

Three layers, in increasing order of how much they actually protect you:

1. Per-call bounds (max bid, max budget, max % change). These catch a typo --
   a bid of "120.00" where "1.20" was meant.

2. The kill switch, `.audit/DISABLE_WRITES`. A FILE, not an environment
   variable, because a long-lived stdio server never sees an env var exported
   after it started, whereas `touch .audit/DISABLE_WRITES` from another terminal
   halts every apply_* within one tool call.

3. Per-session counters. These are the ones that matter. A per-call limit is no
   defence at all against a loop making 400 individually-legal changes, and that
   is the failure mode an agent has and a human does not.

Defaults live here as code constants and are overridden by guardrails.toml,
which is committed, so changing a limit shows up as a diff rather than as an
export someone did once.
"""

from __future__ import annotations

import os
import threading
import tomllib
from dataclasses import dataclass, field
from decimal import Decimal
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
    allowed_campaign_ids: frozenset[int] = field(default_factory=frozenset)
    allowed_currencies: frozenset[str] = field(default_factory=frozenset)
    source: str = "defaults"


def _coerce(key: str, value: object) -> object:
    if key in (
        "max_bid",
        "max_daily_budget",
        "max_projected_delta_per_day",
        "max_projected_session_delta",
    ):
        return Decimal(str(value))
    if key in ("max_bid_increase_pct", "max_budget_change_pct"):
        return float(value)
    if key in ("max_entities_per_call", "max_applies_per_session"):
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
    AUDIT_DIR.mkdir(mode=0o700, exist_ok=True)
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

    def would_exceed(self, projected_delta: Decimal) -> str | None:
        """The reason this apply would breach a session budget, or None."""
        with self._lock:
            if self.applies + 1 > self._limits.max_applies_per_session:
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

    def record(self, projected_delta: Decimal) -> None:
        with self._lock:
            self.applies += 1
            self.projected_delta += max(projected_delta, Decimal("0"))

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "applies_this_session": self.applies,
                "max_applies_per_session": self._limits.max_applies_per_session,
                "projected_session_delta": str(self.projected_delta),
                "max_projected_session_delta": str(self._limits.max_projected_session_delta),
            }


# --- per-change checks -------------------------------------------------------


@dataclass
class Check:
    name: str
    passed: bool
    detail: str

    def render(self) -> str:
        return f"{'OK  ' if self.passed else 'FAIL'} {self.name}: {self.detail}"


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


def check_count(limits: Limits, count: int) -> Check:
    passed = count <= limits.max_entities_per_call
    return Check(
        "entities_per_call",
        passed,
        f"{count} entit{'y' if count == 1 else 'ies'} vs max_entities_per_call "
        f"{limits.max_entities_per_call}" + ("" if passed else " -- too many"),
    )


def check_projected_delta(limits: Limits, projected: Decimal) -> Check:
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
