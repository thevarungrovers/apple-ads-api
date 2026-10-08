"""Change previews: what a human is actually approving, and the token that binds it.

A preview is the text that sits directly above the permission prompt in the
transcript. Four of its fields are what turn an approval into a judgement rather
than a reflex, and they are the reason this module exists rather than the tools
just returning `{"id": 123, "bid": "1.40"}`:

  entity_name                 -- "brand term" vs "competitor brand term"
  path                        -- which campaign and ad group it sits in
  entity_serving_status       -- whether THIS entity can spend money today. Not
                                 the campaign's: a keyword under a paused ad
                                 group cannot spend however healthy the campaign
                                 looks, and reading the campaign's status alone
                                 would warn about live traffic that does not exist
  projected_daily_spend_delta -- roughly how much more per day, as an upper bound

`preview_token` is the other half. Every apply_* demands one, minted by its
matching preview, and refuses when it is missing, unknown, expired, or when the
entity's CURRENT value no longer matches the `before` the preview recorded. That
last check is a TOCTOU guard: between the preview and the approval, someone in
the Apple Ads UI may have moved the same bid. It also guarantees the readable
preview is always in the transcript immediately above the prompt -- an apply
cannot be called cold.

Projections are deliberately labelled an upper bound. They assume tap volume is
unchanged by the change itself, which is wrong in the direction of overstating:
  bid    -> last7d_taps / 7 * (new - old)
  budget -> exactly the budget delta
  pause  -> -(last7d_spend / 7)
"""

from __future__ import annotations

import datetime as dt
import secrets
import threading
from decimal import Decimal
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from apple_ads_mcp.guardrails import Check

# Long enough to read a preview and think; short enough that an approval clicked
# much later is not silently acting on a stale picture of the account.
TOKEN_TTL = dt.timedelta(minutes=10)


class Metrics7d(BaseModel):
    """What this entity did over the last 7 days, used for the projection."""

    spend: str = Field(description="Total spend over the window, account currency")
    taps: int = Field(description="Total taps over the window")
    impressions: int = Field(description="Total impressions over the window")
    installs: int = Field(description="Total installs over the window")
    days: int = Field(description="Days in the window the figures cover")


class ChangePreview(BaseModel):
    """One proposed change, rendered for a human to approve or refuse."""

    preview_token: str = Field(
        description="Pass this to the matching apply_* tool. Expires in 10 minutes."
    )
    tool: str = Field(description="The apply_* tool this preview authorises")
    entity_type: str = Field(description="campaign | ad_group | keyword | negative_keyword")
    entity_id: str = Field(description="Apple's numeric id, as a string")
    entity_name: str = Field(description="The name or keyword text, not just the id")
    path: str = Field(description="Campaign 'X' > AdGroup 'Y' > Keyword 'z'")
    field: str = Field(description="Which field changes")
    before: str = Field(description="Current value, formatted")
    after: str = Field(description="Proposed value, formatted")
    pct_change: str | None = Field(
        default=None, description="Relative change, where both values are numeric"
    )
    campaign_id: str | None = Field(default=None)
    campaign_name: str | None = Field(default=None)
    campaign_daily_budget: str | None = Field(default=None)
    campaign_serving_status: str | None = Field(
        default=None, description="The CAMPAIGN's serving status"
    )
    entity_serving_status: str | None = Field(
        default=None,
        description="This entity's OWN serving status -- RUNNING means it can spend "
        "money today. A keyword under a paused ad group reads AD_GROUP_ON_HOLD "
        "however healthy the campaign looks.",
    )
    last_7d: Metrics7d | None = Field(default=None)
    projected_daily_spend_delta: str = Field(
        description="Upper bound on the extra spend per day if approved; "
        "negative means less spend"
    )
    guardrail_checks: list[str] = Field(
        default_factory=list, description="One line per bound, OK or FAIL"
    )
    blocked: bool = Field(description="True when a guardrail failed; apply_* will refuse")
    warnings: list[str] = Field(default_factory=list)
    note: str = Field(
        default="",
        description="What the numbers mean and what approving this does",
    )


class BulkChangePreview(BaseModel):
    """Several changes approved as one unit."""

    preview_token: str
    tool: str
    entity_type: str
    count: int
    changes: list[ChangePreview]
    total_projected_daily_spend_delta: str
    guardrail_checks: list[str] = Field(default_factory=list)
    blocked: bool
    warnings: list[str] = Field(default_factory=list)
    note: str = ""


class _PendingChange:
    """Server-side half of a preview: what the apply is allowed to do."""

    __slots__ = ("token", "tool", "created_at", "items", "projected_delta", "blocked", "payload")

    def __init__(
        self,
        token: str,
        tool: str,
        items: list[dict[str, Any]],
        projected_delta: Decimal,
        blocked: bool,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.token = token
        self.tool = tool
        self.created_at = dt.datetime.now(dt.timezone.utc)
        self.items = items
        self.projected_delta = projected_delta
        self.blocked = blocked
        self.payload = payload or {}

    def expired(self) -> bool:
        return dt.datetime.now(dt.timezone.utc) - self.created_at > TOKEN_TTL

    def age_seconds(self) -> int:
        return int((dt.datetime.now(dt.timezone.utc) - self.created_at).total_seconds())


class PreviewStore:
    """Pending previews, held for the server's lifetime.

    In-memory on purpose. A token that survived a restart would be a token whose
    `before` snapshot was taken against an account state nobody has looked at
    since, which is exactly what the TOCTOU check exists to prevent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, _PendingChange] = {}

    def mint(
        self,
        tool: str,
        items: list[dict[str, Any]],
        projected_delta: Decimal,
        blocked: bool,
        payload: dict[str, Any] | None = None,
    ) -> str:
        token = f"pv_{secrets.token_urlsafe(16)}"
        with self._lock:
            self._evict_expired()
            self._pending[token] = _PendingChange(
                token, tool, items, projected_delta, blocked, payload
            )
        return token

    def take(self, token: str, tool: str) -> _PendingChange:
        """Consume a token, or raise a ToolError saying exactly what is wrong.

        Single-use: a consumed token cannot approve a second write. Without that,
        one approval of "raise this bid to 1.40" would authorise the same call
        again on the next turn, against a bid that is now 1.40.
        """
        with self._lock:
            self._evict_expired()
            pending = self._pending.get(token)
            if pending is None:
                raise ToolError(
                    f"preview_token {token!r} is unknown or already used. Run the "
                    f"matching preview_* tool and pass the token it returns. Tokens "
                    f"are single-use and expire after {int(TOKEN_TTL.total_seconds() // 60)} "
                    f"minutes."
                )
            if pending.tool != tool:
                raise ToolError(
                    f"preview_token was minted for {pending.tool}, not {tool}. A preview "
                    f"authorises one specific change; it is not a general permission."
                )
            if pending.expired():
                del self._pending[token]
                raise ToolError(
                    f"preview_token expired {pending.age_seconds()}s after it was minted. "
                    f"The account may have moved since. Re-run the preview."
                )
            if pending.blocked:
                del self._pending[token]
                raise ToolError(
                    "that preview was blocked by a guardrail, so it cannot be applied. "
                    "Re-read its guardrail_checks: the FAIL line says which bound it "
                    "breached."
                )
            del self._pending[token]
            return pending

    def _evict_expired(self) -> None:
        for token in [t for t, p in self._pending.items() if p.expired()]:
            del self._pending[token]

    def pending_count(self) -> int:
        with self._lock:
            self._evict_expired()
            return len(self._pending)


# --- projection ---------------------------------------------------------------


def project_bid_change(
    taps_7d: int, before: Decimal | None, after: Decimal
) -> Decimal:
    """`taps/day * (new - old)`. An upper bound: taps are assumed unchanged.

    They will not be -- a higher bid usually wins more taps, so the real figure
    is larger, and a lower bid wins fewer, so the real saving is smaller. The
    number is useful for spotting a 100x typo, not for forecasting.
    """
    if before is None:
        return Decimal("0")
    taps_per_day = Decimal(taps_7d) / Decimal(7)
    return (taps_per_day * (after - before)).quantize(Decimal("0.01"))


def project_budget_change(before: Decimal | None, after: Decimal) -> Decimal:
    """A budget delta is exact, not an estimate -- it is the cap itself moving."""
    return (after - (before or Decimal("0"))).quantize(Decimal("0.01"))


def project_pause(spend_7d: Decimal) -> Decimal:
    """Pausing stops at most what the entity was spending: `-(spend/7)`."""
    return (-(spend_7d / Decimal(7))).quantize(Decimal("0.01"))


def pct_change(before: Decimal | None, after: Decimal) -> str | None:
    if before is None or before == 0:
        return None
    return f"{float((after - before) / before * 100):+.1f}%"


def render_checks(checks: list[Check]) -> list[str]:
    return [check.render() for check in checks]


def any_failed(checks: list[Check]) -> bool:
    return any(not check.passed for check in checks)
