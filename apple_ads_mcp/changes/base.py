"""The shapes every change kind speaks in.

A *kind* is one sort of change -- a keyword's bid, a campaign's status, a batch
of negative keywords. It knows five things and nothing else:

  * ``propose``  -- read the entity and turn the agent's arguments into a
                    :class:`Proposal`: one JSON-able ITEM per entity (before,
                    after, and whatever the request needs), the bounds it has to
                    clear, the projected spend, and the readable context;
  * ``current``  -- read an item's value again at apply time, spelled exactly as
                    the proposal spelled ``before``, so a change somebody made
                    in the Apple Ads UI in between is caught, not overwritten;
  * ``request``  -- the SDK method and kwargs that make the change;
  * ``observe``  -- what Apple echoed back for each item, which ``landed()``
                    compares with ``after``: HTTP 200 does NOT mean applied;
  * ``inverse``  -- the item that undoes one ledger entry, and the kind whose
                    request sends it.

Everything else -- the token, the kill switch, staleness, the session and daily
caps, the two-phase ledger, the read-back -- lives once, in
:mod:`apple_ads_mcp.changes.engine`. Before it, each of the eight original
pairs hand-rolled its own copy of that sequence, and ``revert_change`` was a
dict of seven more copies. A kind cannot forget a gate it never implements.

ITEMS. An item is a plain dict, because it is carried inside a preview token
and must survive being stored. Every item has these keys, which the engine
reads; a kind adds whatever else its ``request`` needs:

    entity_type  entity_id  entity_name  path  field  before  after
    campaign_id  projected
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from mcp.types import ToolAnnotations

from apple_ads_mcp.guardrails import Check
from apple_ads_mcp.previews import ChangePreview, Metrics7d

# All four hints on every one of these -- see the note in tools_read.py for why
# an omitted hint is worse than a wrong one.
#
# A preview reads Apple, mints a token in memory and writes nothing to the
# account, so it is safe to allowlist: read_only_hint=True. The token it mints is
# single-use, but calling a preview twice just produces a second token and
# changes nothing anybody can observe at Apple's end, so idempotent_hint stays
# True.
PREVIEW = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

# An apply mutates. idempotent_hint=False because a token is single-use: calling
# the same apply twice is not a no-op, it is an error the second time.
# destructive_hint=False on this one: pausing and un-pausing are one call each,
# and adding a negative keyword only ever adds. Marking a reversible action
# destructive trains you to click through the loud prompts.
APPLY = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

# destructive_hint=True only where the action moves money at campaign scale or
# CREATES something: pausing a whole campaign stops every ad group under it, a
# daily budget is the ceiling on the day's spend, and a created entity cannot
# be deleted from here -- only paused.
APPLY_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=True,
)

# A tool that reads Apple and writes ONLY the local ledger (resolve_unknown_change).
# Not read-only -- the ledger it writes decides what the caps count -- but never
# destructive, and idempotent: a second call finds nothing left to settle.
LEDGER_ONLY = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

#: What "before" reads as for something that does not exist yet. Never a value a
#: real entity could hold.
NOTHING = "(none)"

#: The ledger field a creation writes. `revert_change` finds the creating kind
#: by (entity_type, CREATED), and that kind's inverse is a pause, never a delete.
CREATED = "created"


class NothingToUndo(ValueError):
    """Reverting this entry would change nothing -- it is already where the
    inverse would put it (a keyword created PAUSED and never enabled). A batch
    revert skips such rows; a single revert reports it."""


@dataclass
class Outcome:
    """What Apple said about one item, judged by read-back rather than status."""

    landed: bool
    observed: str | None
    #: The id Apple assigned, for a creation. Written into the ledger row's
    #: entity_id so revert and reconcile can find the thing that was made.
    created_id: str | None = None
    error: str | None = None
    #: The requested change LANDED, but the read-back shows something else
    #: moved too (``error`` names it). Recorded PARTIAL, never FAILED: the
    #: change is live and spending, so it counts against the caps and stays
    #: revertible. ``landed`` is True whenever this is.
    partial: bool = False
    #: The request for this item left the process and no answer came back (a
    #: TRANSIENT error part-way through a multi-request change): it may have
    #: landed. Recorded UNKNOWN and counted against the caps as if it had, the
    #: same as the engine does for a whole request. ``landed`` stays False.
    unknown: bool = False

    @property
    def counted(self) -> bool:
        """Charged against the session and daily caps: landed, or may have."""
        return self.landed or self.unknown


@dataclass
class Proposal:
    """What a kind wants to do, before the engine checks and tokenises it."""

    items: list[dict[str, Any]]
    checks: list[Check]
    projected: Decimal
    warnings: list[str] = field(default_factory=list)
    note: str = ""
    #: An ENABLE -- something that resumes or widens delivery. Exempt from the
    #: per-change max_projected_delta_per_day (a 100/day campaign could never be
    #: re-enabled under a 50 limit) and counted in full against the DAILY total
    #: and the session total instead. See guardrails.check_projected_delta.
    is_enable: bool = False

    # -- the readable half of a single-entity preview ------------------------
    entity_type: str = ""
    entity_id: str = ""
    entity_name: str | None = None
    path: str = "-"
    field_name: str = ""
    before: str = ""
    after: str = ""
    before_num: Decimal | None = None
    after_num: Decimal | None = None
    campaign: Any = None
    entity_serving: str | None = None
    metrics: Metrics7d | None = None

    # -- a batch ----------------------------------------------------------------
    #: Per-item previews for a bulk kind, shown under the batch token.
    children: list[ChangePreview] = field(default_factory=list)


class ChangeKind:
    """One sort of change. Subclass, fill in the methods, register it.

    ``propose`` is also the TOOL SIGNATURE: the engine publishes
    ``preview_<name>`` with exactly ``propose``'s parameters (minus ``ctx``) and
    its docstring as the description. Annotate every parameter with
    ``Annotated[..., Field(description=...)]`` the way a hand-written tool would.
    """

    #: preview_<name> / apply_<name>.
    name: str = ""
    preview_title: str = ""
    apply_title: str = ""
    apply_annotations: ToolAnnotations = APPLY
    #: True for a batch: the preview is a BulkChangePreview, the apply a
    #: BulkApplyResult, and the engine adds the entities_per_call check.
    bulk: bool = False
    #: How many applies one of these costs against the session and daily caps.
    #: A creation costs 3.
    cost: int = 1
    #: (entity_type, field) pairs this kind writes to the ledger -- how
    #: revert_change finds the kind that can undo an entry.
    ledger_keys: tuple[tuple[str, str], ...] = ()
    #: Why an entry of this kind cannot be undone, or None if it can.
    not_revertible: str | None = None
    #: True when this kind's revert_projection is a CAMPAIGN's bound (its unused
    #: budget), not the item's own: a batch revert then counts it once per
    #: campaign, on the first item of that campaign that lands.
    revert_bound_per_campaign: bool = False
    #: True when a PARTIAL row of this kind can be reverted: its inverse puts
    #: back only what the change asked for and leaves whatever drifted alone.
    revert_partial: bool = False
    #: What the apply says when Apple answered 200 and changed nothing.
    failure_note: str = (
        "NOT APPLIED. Apple returned success and left the value alone. For a "
        "keyword bid this usually means the ad group is on an automated bid "
        "strategy, where Apple sets the bids and a keyword bid is ignored. "
        "Recorded as failed, so revert_change will refuse it -- there is nothing "
        "to undo."
    )
    success_note: str = ""

    def propose(self, ctx, **kwargs) -> Proposal:  # pragma: no cover - abstract
        raise NotImplementedError

    def current(self, ctx, item: dict[str, Any]) -> str | None:  # pragma: no cover
        """Today's value for ``item``, spelled exactly as ``item['before']``.

        None means "nothing to compare" (a creation), and skips the check.
        """
        raise NotImplementedError

    def request(self, ctx, items: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        """(sdk_method, kwargs) that writes ``items``."""  # pragma: no cover
        raise NotImplementedError

    def observe(self, ctx, response: Any, items: list[dict[str, Any]]) -> list[Outcome]:
        """One Outcome per item, in order."""  # pragma: no cover
        raise NotImplementedError

    def execute(self, ctx, items: list[dict[str, Any]], call) -> list[Outcome]:
        """Send ``items`` and judge each by read-back. One request by default.

        A kind whose change takes more than one request (add a keyword, THEN
        pause the old one) overrides this. A ToolError raised here marks every
        item failed; a kind that has already landed part of its change returns
        an Outcome with ``error`` for the rest instead of raising.
        """
        from apple_ads_mcp.client import WRITE_TIMEOUT

        method, kwargs = self.request(ctx, items)
        response = call(method, _request_timeout=WRITE_TIMEOUT, **kwargs)
        return self.observe(ctx, response, items)

    def landed_projections(
        self, items: list[dict[str, Any]], outcomes: list[Outcome]
    ) -> list[Decimal]:
        """What each item's ledger row records as its projected spend, once the
        outcomes are known: its own positive delta if it landed (or may have:
        an UNKNOWN item is counted as if it had), else 0.

        The daily cap sums these rows and the session counter their total, so
        a partial batch counts exactly what landed. A kind whose bound belongs
        to a GROUP of items (one per campaign) overrides this.
        """
        return [
            max(Decimal(str(item.get("projected") or "0")), Decimal("0")) if o.counted else Decimal("0")
            for item, o in zip(items, outcomes)
        ]

    # -- revert ------------------------------------------------------------------

    def inverse(self, ctx, entry: dict[str, Any]) -> tuple["ChangeKind", dict[str, Any]]:
        """(kind whose request sends it, the item that puts ``entry`` back)."""
        raise ValueError(
            self.not_revertible
            or f"{entry.get('field')!r} on {entry.get('entity_type')} cannot be "
            "reverted automatically. Change it back with the matching preview_* tool."
        )

    def revert_projection(self, ctx, entry: dict[str, Any], item: dict[str, Any]) -> tuple[Decimal, bool]:
        """(projected added daily spend, is_enable) for reverting ``entry``.

        A revert is not free: reverting a budget CUT or a PAUSE puts spend back.
        The default is the inverse of what the original projected -- a cut of 10
        reverts as +10 -- counted like an enable, since it restores a state the
        account already had. Status kinds override this with a real enable bound.
        """
        try:
            original = Decimal(str(entry.get("projected_daily_spend_delta") or "0"))
        except Exception:
            original = Decimal("0")
        restored = max(-original, Decimal("0"))
        return restored, restored > 0


def bound_once_per_campaign(
    items: list[dict[str, Any]], outcomes: list[Outcome]
) -> list[Decimal]:
    """Projected spend per item when a bound belongs to a CAMPAIGN, not an item.

    An item carrying ``campaign_bound`` records that bound once per campaign,
    on the first of the campaign's items that landed (or may have); the rest
    of that campaign's items record 0. An item without one records its own
    positive ``projected`` if it counts. Nothing that did not land counts.
    """
    seen: set[Any] = set()
    recorded = []
    for entry, outcome in zip(items, outcomes):
        value = Decimal("0")
        if outcome.counted:
            if "campaign_bound" in entry:
                campaign = entry.get("campaign_id")
                if campaign not in seen:
                    seen.add(campaign)
                    value = max(Decimal(str(entry.get("campaign_bound") or "0")), Decimal("0"))
            else:
                value = max(Decimal(str(entry.get("projected") or "0")), Decimal("0"))
        recorded.append(value)
    return recorded


def item(
    *,
    entity_type: str,
    entity_id: Any,
    entity_name: str | None,
    path: str,
    field: str,
    before: str | None,
    after: str,
    campaign_id: Any,
    projected: Decimal = Decimal("0"),
    **extra: Any,
) -> dict[str, Any]:
    """An item dict with the keys the engine reads, plus the kind's own."""
    return {
        "entity_type": entity_type,
        "entity_id": None if entity_id is None else str(entity_id),
        "entity_name": entity_name,
        "path": path,
        "field": field,
        "before": before,
        "after": after,
        "campaign_id": int(campaign_id) if campaign_id not in (None, "") else None,
        "projected": str(projected),
        **extra,
    }
