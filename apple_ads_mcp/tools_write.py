"""Write tools: a `preview_*` and an `apply_*` for each change, and `revert_change`.

SEPARATE TOOLS, NOT A `dry_run` PARAMETER. This is the most consequential choice
in the design. Claude Code's permission rules key on the tool NAME, so separate
names let you permanently allowlist every `preview_*` while never allowlisting an
`apply_*`. With a `dry_run` flag, one "always allow" clicked during a harmless
preview would silently authorise every future real write -- exactly the failure
this is guarding against.

Every `apply_*` demands the `preview_token` its own preview minted. That buys
three things at once: a readable preview always sits directly above the approval
prompt in the transcript, an apply cannot be called cold, and the recorded
`before` is re-checked against the entity's CURRENT value so a change someone
made in the Apple Ads UI in between is caught rather than silently overwritten.

`destructive_hint=True` is set only where the action moves money in a way that is
not a one-call undo. Pausing a keyword is reversible, so marking it destructive
would train you to click through the loud prompts -- which is how the loud
prompts stop working.

NOT EXPOSED, deliberately:
  - negative_keywords_id_delete -- pausing achieves the same outcome reversibly,
    and deletion would be the only irreversible operation in scope.
  - apply_daily_budget_recommendations -- one call that moves real money with no
    preview, no bound and no ledger line.
  - shared budgets -- shared_budgets_id_put takes no x_ap_context argument at
    all, so there is no way to say which ad account an update belongs to.

THE AD GROUP DEFAULT BID was on that list until 2026-10-09, on the grounds that
`AdGroupUpdate` has no `defaultBid` field and the bid only *appeared* to live at
`bidStrategy.bid`. Verified against the live API on that date: GET /v1/adgroups/
{id} returns `bidStrategy: {bidStrategyType, bidStrategyGoal, bid}`, and
`AdGroupUpdate.bid_strategy` mirrors it, so the pair is now exposed. The write
sends bidStrategyType and bidStrategyGoal back unchanged alongside the new bid,
because bidStrategy is a NESTED object: a PUT carrying only `bid` risks Apple
replacing the whole object and dropping the strategy with it, which would move
the ad group onto a different bidding model nobody approved. It is the only bid
a Search Tab or Search Match ad group has, since neither carries keywords.

THE PAIRS ARE GENERATED. Each change is a `ChangeKind` in
`apple_ads_mcp/changes/`, and `changes.engine.register_pair` publishes its
`preview_<name>` / `apply_<name>` with the one shared preview -> apply ->
revert sequence. This module only wires the engine to the server state and
publishes `revert_change`.

Enforcement is structural: there is no `request(method, path, body)` passthrough,
so an unregistered operation is unreachable rather than merely undocumented.
`tests/test_tool_surface.py` asserts the registered tool names equal an expected
frozenset, so a later edit cannot widen the surface by accident.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import Field

from apple_ads_mcp.changes import all_kinds
from apple_ads_mcp.changes.base import APPLY, APPLY_DESTRUCTIVE, LEDGER_ONLY, PREVIEW
from apple_ads_mcp.changes.context import ChangeContext
from apple_ads_mcp.changes.engine import Engine, register_pair
from apple_ads_mcp.client import call
from apple_ads_mcp.guardrails import read_only_mode, writes_disabled
from apple_ads_mcp.previews import ApplyResult, BulkApplyResult, SettleResult

__all__ = [
    "APPLY",
    "APPLY_DESTRUCTIVE",
    "PREVIEW",
    "ApplyResult",
    "BulkApplyResult",
    "register",
]

#: The engine the last register() call built. Tests and revert reach the kinds
#: through it.
ENGINE: Engine | None = None


def register(mcp, state) -> Engine:
    """Publish every change kind's pair, and revert_change.

    Read-only mode: the `preview_*` tools still register, because a preview
    writes nothing and is the whole point of being able to say "here is what I
    would change". Every `apply_*` and `revert_change` is simply never handed
    to the registrar, so they are absent from tools/list and unreachable over
    MCP -- not refused at call time, ABSENT. See guardrails.read_only_mode().
    """
    global ENGINE
    read_only = read_only_mode()

    def apply_tool(**kwargs):
        """`mcp.tool(...)` for a write tool, or a no-op when read-only.

        Returning the bare function leaves it defined and callable in-process --
        which the tests rely on -- while registering nothing, so nothing crosses
        the MCP boundary.
        """
        if read_only:
            return lambda fn: fn
        return mcp.tool(**kwargs)

    # Resolved through this module's globals at CALL time, so a test that
    # replaces tools_write.call or tools_write.writes_disabled reaches the engine.
    ctx = ChangeContext(
        state,
        call=lambda method, /, **kwargs: call(method, **kwargs),
        writes_disabled=lambda: writes_disabled(),
    )
    engine = Engine(ctx)
    for kind in all_kinds():
        register_pair(mcp, engine, kind, apply_tool=apply_tool)

    @apply_tool(
        title="Undo one change from the ledger (writes the old value back)",
        annotations=APPLY,
    )
    def revert_change(
        entry_id: Annotated[
            str,
            Field(
                description="entry_id from list_my_changes or from an apply result; "
                "a batch id undoes every item of that batch that landed"
            ),
        ],
    ) -> ApplyResult:
        """Put a single previous change back the way it was.

        Refuses unless that entry is recorded as `applied`, and re-reads the
        entity first: if its current value is no longer what the entry said it
        was left at, somebody else has moved it since and reverting would silently
        overwrite THEIR change. Writes a new ledger entry rather than erasing the
        old one -- the ledger is append-only.

        A creation (a negative keyword, and later keywords, ads and campaigns) is
        undone by PAUSING what was created. Nothing is ever deleted.
        """
        return engine.revert(entry_id)

    # Published with the writes, so ABSENT in read-only mode. It writes nothing
    # to Apple, but it does write the ledger, and settling a row as failed
    # releases what it held of the daily cap: bookkeeping that belongs to the
    # install that can write. A read-only process creates no unknown rows of its
    # own, and reconcile_ledger still lists any it can see.
    @apply_tool(
        title="Settle a change whose outcome is unknown, by read-back (writes only the local ledger)",
        annotations=LEDGER_ONLY,
    )
    def resolve_unknown_change(
        entry_id: Annotated[
            str,
            Field(
                description="entry_id of a change recorded as 'unknown' (a timed-out "
                "write), or a batch id to settle every unknown item in it"
            ),
        ],
    ) -> SettleResult:
        """Find out whether a timed-out write landed, and record it. Reverts nothing.

        Reads the entity back from Apple and records the entry as 'applied' (Apple
        shows the value it asked for) or 'failed' (Apple still shows the old one).
        Writes NOTHING to Apple -- only the local ledger. Refuses a creation
        (Apple's id never came back) and a value that is neither before nor
        after. 'applied' can be recorded at once; 'failed' only once the write
        is 5 minutes old (Apple's read can lag -- the refusal says when to try
        again), and never when a later ledger row changed the same field. Use
        this to settle an unknown row; use revert_change only when you
        actually want the change undone.
        """
        return engine.settle(entry_id)

    ENGINE = engine
    return engine
