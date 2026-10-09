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

Enforcement is structural: there is no `request(method, path, body)` passthrough,
so an unregistered operation is unreachable rather than merely undocumented.
`tests/test_tool_surface.py` asserts the registered tool names equal an expected
frozenset, so a later edit cannot widen the surface by accident.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from apple_ads_platform import (
    AdGroupStatus,
    AdGroupUpdate,
    BidStrategyGoal,
    BidStrategyType,
    BidStrategyUpdate,
    BulkKeywordUpdate,
    BulkNegativeKeywordCreate,
    CampaignStatus,
    CampaignUpdate,
    DailyBudgetUpdate,
    KeywordStatus,
    KeywordUpdate,
    KeywordUpdateBulkRequest,
    KeywordUpdateBulkRequestItem,
    NegativeKeywordCreateBulkRequest,
    NegativeKeywordCreateBulkRequestItem,
    NegativeKeywordStatus,
    NegativeKeywordUpdate,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from apple_ads_mcp import client, ledger
from apple_ads_mcp.client import (
    WRITE_TIMEOUT,
    call,
    context_header,
    enum_value,
    entity_path,
    format_money,
    landed,
    money_amount,
    money_currency,
    parse_decimal,
    to_money,
    unwrap,
)
from apple_ads_mcp.guardrails import (
    Check,
    check_bid_settable,
    check_campaign_in_scope,
    check_ceiling,
    check_count,
    check_currency,
    check_known_status,
    check_pct_increase,
    check_projected_delta,
    writes_disabled,
)
from apple_ads_mcp.previews import (
    BulkChangePreview,
    ChangePreview,
    Metrics7d,
    any_failed,
    pct_change,
    project_bid_change,
    project_budget_change,
    project_pause,
    render_checks,
)
from apple_ads_mcp.tools_read import entity_metrics_7d

# A preview is read-only and open to the world; it is safe to allowlist.
PREVIEW = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)

# An apply mutates. idempotent_hint=False because a token is single-use: calling
# the same apply twice is not a no-op, it is an error the second time.
APPLY = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=True
)
APPLY_DESTRUCTIVE = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=True
)


class ApplyResult(BaseModel):
    """What actually happened, as opposed to what was proposed."""

    applied: bool
    entry_id: str = Field(description="Pass this to revert_change to undo it")
    tool: str
    entity_type: str
    entity_id: str
    entity_name: str | None = None
    path: str | None = None
    field: str
    before: str
    after: str
    observed_after: str | None = Field(
        default=None, description="What Apple reports now, read back from its response"
    )
    projected_daily_spend_delta: str
    session: dict[str, str]
    failures: list[str] = Field(default_factory=list)
    note: str = ""


class BulkApplyResult(BaseModel):
    applied: bool = Field(description="False when nothing landed; see succeeded/failed")
    entry_id: str
    tool: str
    succeeded: int
    failed: int
    results: list[str] = Field(description="One line per item, in request order")
    failures: list[str] = Field(default_factory=list)
    projected_daily_spend_delta: str
    session: dict[str, str]
    note: str


def register(mcp, state) -> None:  # noqa: C901 -- a flat list of tool definitions
    # --- shared machinery ----------------------------------------------------

    def guard_writes() -> None:
        """The kill switch. Checked inside every apply, never cached.

        A FILE rather than an env var: a long-lived stdio server never sees a
        variable exported after it started, but `touch .audit/DISABLE_WRITES`
        from another terminal halts the next apply_* within one tool call.
        """
        reason = writes_disabled()
        if reason:
            raise ToolError(
                f"writes are disabled: {reason}. Remove .audit/DISABLE_WRITES to "
                f"re-enable them."
            )

    def keyword_context(keyword: Any) -> tuple[Any, Any]:
        """(ad_group, campaign) for a keyword -- the readable half of a preview."""
        ad_group = client.get_ad_group(keyword.ad_group_id) if keyword.ad_group_id else None
        campaign = client.get_campaign(keyword.campaign_id) if keyword.campaign_id else None
        return ad_group, campaign

    def campaign_facts(campaign: Any) -> tuple[str | None, str | None, str | None]:
        if campaign is None:
            return None, None, None
        budget = campaign.daily_budget.value if campaign.daily_budget else None
        return (
            campaign.name,
            format_money(budget) if budget else None,
            enum_value(campaign.display_status),
        )

    def metrics_warning(metrics_error: str | None) -> list[str]:
        """A projection built on a failed report is a confident 0.00. Say so."""
        if not metrics_error:
            return []
        return [
            "Could not read the last 7 days for this entity, so the 7d figures and "
            f"projected_daily_spend_delta are 0 by default, NOT by measurement: "
            f"{metrics_error[:200]}"
        ]

    def metrics_block(spend: Decimal, taps: int, impressions: int, installs: int) -> Metrics7d:
        return Metrics7d(
            spend=str(spend),
            taps=taps,
            impressions=impressions,
            installs=installs,
            days=7,
        )

    def serving_warning(entity_status: str | None, kind: str) -> list[str]:
        """Warn from the ENTITY's serving status, not its campaign's.

        A keyword under a paused ad group reads AD_GROUP_ON_HOLD while its
        campaign reads RUNNING. Warning off the campaign would shout about live
        traffic that this entity cannot receive -- and a warning that cries wolf
        is one people learn to scroll past.
        """
        if entity_status == "RUNNING":
            return [f"This {kind} IS SERVING TODAY -- the change takes effect on live traffic."]
        if entity_status in (None, "unknown_default_open_api"):
            return [f"Could not determine whether this {kind} is serving. Treat it as live."]
        return [
            f"This {kind} is {entity_status}, so it is not spending right now. The "
            f"change takes effect whenever it next serves."
        ]

    def build_preview(
        *,
        tool: str,
        entity_type: str,
        entity_id: int,
        entity_name: str | None,
        path: str,
        field: str,
        before: str,
        after: str,
        before_num: Decimal | None,
        after_num: Decimal | None,
        campaign: Any,
        entity_serving: str | None,
        metrics: Metrics7d | None,
        projected: Decimal,
        checks: list[Check],
        warnings: list[str],
        note: str,
        item: dict[str, Any],
    ) -> ChangePreview:
        checks = list(checks) + [check_projected_delta(state.limits, projected)]
        blocked = any_failed(checks)
        campaign_name, campaign_budget, campaign_serving = campaign_facts(campaign)
        token = state.previews.mint(tool, [item], projected, blocked)
        return ChangePreview(
            preview_token=token,
            tool=tool,
            entity_type=entity_type,
            entity_id=str(entity_id),
            entity_name=entity_name or "(unnamed)",
            path=path,
            field=field,
            before=before,
            after=after,
            pct_change=pct_change(before_num, after_num) if after_num is not None else None,
            campaign_id=str(campaign.id) if campaign is not None else None,
            campaign_name=campaign_name,
            campaign_daily_budget=campaign_budget,
            campaign_serving_status=campaign_serving,
            entity_serving_status=entity_serving,
            last_7d=metrics,
            projected_daily_spend_delta=str(projected),
            guardrail_checks=render_checks(checks),
            blocked=blocked,
            warnings=warnings,
            note=note,
        )

    def begin_apply(pending, projected: Decimal) -> None:
        """Session budget check. Runs after the token is consumed, before the call."""
        guard_writes()
        reason = state.counters.would_exceed(projected)
        if reason:
            raise ToolError(reason)

    def confirm_unchanged(current: str, recorded: str, what: str) -> None:
        """TOCTOU. Refuse when the entity moved between preview and approval."""
        if current != recorded:
            raise ToolError(
                f"{what} is now {current!r}, but the preview recorded {recorded!r}. "
                f"Something changed it in between -- possibly a person in the Apple "
                f"Ads UI. Re-run the preview and look at the new numbers before "
                f"deciding again."
            )

    def finish(
        entry_id: str,
        *,
        tool: str,
        entity_type: str,
        entity_id: int,
        entity_name: str | None,
        path: str,
        field: str,
        before: str,
        after: str,
        observed_after: str | None,
        projected: Decimal,
        note: str = "",
        failure_note: str = "",
    ) -> ApplyResult:
        applied = landed(observed_after, after)
        ledger.write_outcome(
            entry_id,
            outcome=ledger.APPLIED if applied else ledger.FAILED,
            observed_after=observed_after,
            detail=""
            if applied
            else f"Apple answered 200 but echoed {observed_after!r}, not {after!r}",
        )
        if applied:
            state.counters.record(projected)
        failures = (
            []
            if applied
            else [
                f"Apple accepted the request but {field} is still {observed_after!r}, "
                f"not {after!r}. Nothing changed."
            ]
        )
        return ApplyResult(
            applied=applied,
            entry_id=entry_id,
            tool=tool,
            entity_type=entity_type,
            entity_id=str(entity_id),
            entity_name=entity_name,
            path=path,
            field=field,
            before=before,
            after=after,
            observed_after=observed_after,
            projected_daily_spend_delta=str(projected),
            session={k: str(v) for k, v in state.counters.snapshot().items()},
            failures=failures,
            note=(
                (note or f"Undo with revert_change(entry_id='{entry_id}').")
                if applied
                else (
                    failure_note
                    or (
                        "NOT APPLIED. Apple returned success and left the value alone. "
                        "For a keyword bid this usually means the ad group is on an "
                        "automated bid strategy, where Apple sets the bids and a "
                        "keyword bid is ignored. Recorded as failed, so revert_change "
                        "will refuse it -- there is nothing to undo."
                    )
                )
            ),
        )

    def run_write(entry_id: str, method: str, **kwargs):
        """Call Apple, and record a `failed` outcome if it raises.

        The intent line is already on disk by the time this runs, so a crash or a
        timeout between here and the outcome line leaves an entry with no
        outcome -- which is the evidence that a mutation may have landed.
        """
        try:
            return call(method, _request_timeout=WRITE_TIMEOUT, **kwargs)
        except ToolError as exc:
            ledger.write_outcome(entry_id, outcome=ledger.FAILED, detail=str(exc))
            raise

    # --- keyword bid ---------------------------------------------------------

    @mcp.tool(title="Preview a keyword bid change (no write)", annotations=PREVIEW)
    def preview_keyword_bid(
        keyword_id: Annotated[int, Field(description="Apple keyword id")],
        new_bid: Annotated[
            str,
            Field(description='New bid as a decimal string in major units, e.g. "1.20"'),
        ],
    ) -> ChangePreview:
        """Show what changing one keyword's bid would do. Writes nothing.

        Returns a preview_token for apply_keyword_bid. Check
        campaign_serving_status and projected_daily_spend_delta before proposing
        it to a human.
        """
        after_num = parse_decimal(new_bid, "new_bid")
        keyword = client.get_keyword(keyword_id)
        ad_group, campaign = keyword_context(keyword)
        before_num = money_amount(keyword.bid)
        currency = money_currency(keyword.bid) or None

        spend, taps, impressions, installs, metrics_error = entity_metrics_7d(
            "apps_keyword_reports", "keywordId", keyword_id, keyword.campaign_id
        )
        projected = project_bid_change(taps, before_num, after_num)

        checks = [
            check_campaign_in_scope(state.limits, keyword.campaign_id),
            check_currency(state.limits, currency),
            check_ceiling("max_bid", after_num, state.limits.max_bid, currency or ""),
            check_pct_increase(
                "max_bid_increase_pct", before_num, after_num, state.limits.max_bid_increase_pct
            ),
            check_known_status("keyword_status_known", enum_value(keyword.status)),
            check_bid_settable(_bid_strategy_type(ad_group)),
        ]
        entity_serving = enum_value(keyword.display_status)
        warnings = serving_warning(entity_serving, "keyword") + metrics_warning(metrics_error)
        if enum_value(keyword.status) != "ENABLED":
            warnings.append("The keyword itself is PAUSED, so this bid will not spend until it is enabled.")

        return build_preview(
            tool="apply_keyword_bid",
            entity_type="keyword",
            entity_id=keyword_id,
            entity_name=keyword.text,
            path=entity_path(campaign, ad_group, keyword.text),
            field="bid",
            before=format_money(keyword.bid),
            after=f"{after_num} {currency or ''}".strip(),
            before_num=before_num,
            after_num=after_num,
            campaign=campaign,
            entity_serving=entity_serving,
            metrics=metrics_block(spend, taps, impressions, installs),
            projected=projected,
            checks=checks,
            warnings=warnings,
            note=(
                "projected_daily_spend_delta is an UPPER BOUND: it assumes tap "
                "volume is unchanged, which a bid change is precisely intended to "
                "alter. Use it to catch a 100x typo, not as a forecast."
            ),
            item={
                "keyword_id": keyword_id,
                "before": str(before_num) if before_num is not None else None,
                "after": str(after_num),
                "currency": currency,
                "text": keyword.text,
                "campaign_id": keyword.campaign_id,
                "path": entity_path(campaign, ad_group, keyword.text),
            },
        )

    @mcp.tool(
        title="Apply a keyword bid change (CHANGES WHAT THIS KEYWORD COSTS)",
        annotations=APPLY,
    )
    def apply_keyword_bid(
        preview_token: Annotated[
            str, Field(description="The token returned by preview_keyword_bid")
        ],
    ) -> ApplyResult:
        """Write the bid change the matching preview described. Live account."""
        pending = state.previews.take(preview_token, "apply_keyword_bid")
        item = pending.items[0]
        begin_apply(pending, pending.projected_delta)

        keyword = client.get_keyword(item["keyword_id"])
        current = money_amount(keyword.bid)
        confirm_unchanged(
            str(current) if current is not None else "none",
            item["before"] if item["before"] is not None else "none",
            f"keyword {item['keyword_id']} bid",
        )

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id,
            tool="apply_keyword_bid",
            entity_type="keyword",
            entity_id=item["keyword_id"],
            entity_name=item["text"],
            path=item["path"],
            field="bid",
            before=item["before"],
            after=item["after"],
            campaign_id=item["campaign_id"],
            projected_daily_spend_delta=str(pending.projected_delta),
        )
        response = run_write(
            entry_id,
            "keywords_id_put",
            id=str(item["keyword_id"]),
            x_ap_context=context_header(),
            keyword_update=KeywordUpdate(bid=to_money(item["after"], item["currency"])),
        )
        updated = unwrap(response, "keyword update")
        return finish(
            entry_id,
            tool="apply_keyword_bid",
            entity_type="keyword",
            entity_id=item["keyword_id"],
            entity_name=item["text"],
            path=item["path"],
            field="bid",
            before=item["before"] or "none",
            after=item["after"],
            observed_after=format_money(getattr(updated, "bid", None)) if updated else None,
            projected=pending.projected_delta,
        )

    # --- keyword bids, bulk --------------------------------------------------

    @mcp.tool(title="Preview several keyword bid changes at once (no write)", annotations=PREVIEW)
    def preview_keyword_bids_bulk(
        keyword_ids: Annotated[
            list[int], Field(description="Keyword ids to change, in the same order as new_bids")
        ],
        new_bids: Annotated[
            list[str],
            Field(description='New bids as decimal strings, e.g. ["1.20", "0.85"]'),
        ],
    ) -> BulkChangePreview:
        """Show what a batch of bid changes would do. Writes nothing.

        Every item is previewed individually and the projected deltas are summed,
        so one out-of-bounds bid blocks the whole batch rather than slipping
        through inside a total that looks reasonable.
        """
        if len(keyword_ids) != len(new_bids):
            raise ToolError(
                f"keyword_ids has {len(keyword_ids)} entries and new_bids has "
                f"{len(new_bids)}; they are positional and must be the same length."
            )
        if not keyword_ids:
            raise ToolError("keyword_ids is empty; there is nothing to preview.")

        count_check = check_count(state.limits, len(keyword_ids))
        changes: list[ChangePreview] = []
        items: list[dict[str, Any]] = []
        total = Decimal("0")
        all_checks: list[Check] = [count_check]

        for keyword_id, raw_bid in zip(keyword_ids, new_bids):
            after_num = parse_decimal(raw_bid, f"new_bids[{keyword_id}]")
            keyword = client.get_keyword(keyword_id)
            ad_group, campaign = keyword_context(keyword)
            before_num = money_amount(keyword.bid)
            currency = money_currency(keyword.bid)
            spend, taps, impressions, installs, metrics_error = entity_metrics_7d(
                "apps_keyword_reports", "keywordId", keyword_id, keyword.campaign_id
            )
            projected = project_bid_change(taps, before_num, after_num)
            total += projected
            checks = [
                check_campaign_in_scope(state.limits, keyword.campaign_id),
                check_currency(state.limits, currency),
                check_ceiling("max_bid", after_num, state.limits.max_bid, currency or ""),
                check_pct_increase(
                    "max_bid_increase_pct",
                    before_num,
                    after_num,
                    state.limits.max_bid_increase_pct,
                ),
                check_known_status("keyword_status_known", enum_value(keyword.status)),
                check_bid_settable(_bid_strategy_type(ad_group)),
            ]
            all_checks.extend(checks)
            path = entity_path(campaign, ad_group, keyword.text)
            changes.append(
                ChangePreview(
                    preview_token="(covered by the batch token)",
                    tool="apply_keyword_bids_bulk",
                    entity_type="keyword",
                    entity_id=str(keyword_id),
                    entity_name=keyword.text or "(unnamed)",
                    path=path,
                    field="bid",
                    before=format_money(keyword.bid),
                    after=f"{after_num} {currency or ''}".strip(),
                    pct_change=pct_change(before_num, after_num),
                    campaign_id=str(keyword.campaign_id) if keyword.campaign_id else None,
                    campaign_name=getattr(campaign, "name", None),
                    campaign_daily_budget=campaign_facts(campaign)[1],
                    campaign_serving_status=enum_value(getattr(campaign, "display_status", None)),
                    entity_serving_status=enum_value(keyword.display_status),
                    last_7d=metrics_block(spend, taps, impressions, installs),
                    projected_daily_spend_delta=str(projected),
                    guardrail_checks=render_checks(checks),
                    blocked=any_failed(checks),
                    warnings=[],
                )
            )
            items.append(
                {
                    "keyword_id": keyword_id,
                    "before": str(before_num) if before_num is not None else None,
                    "after": str(after_num),
                    "currency": currency,
                    "text": keyword.text,
                    "campaign_id": keyword.campaign_id,
                    "path": path,
                }
            )

        all_checks.append(check_projected_delta(state.limits, total))
        blocked = any_failed(all_checks)
        token = state.previews.mint("apply_keyword_bids_bulk", items, total, blocked)
        return BulkChangePreview(
            preview_token=token,
            tool="apply_keyword_bids_bulk",
            entity_type="keyword",
            count=len(items),
            changes=changes,
            total_projected_daily_spend_delta=str(total),
            guardrail_checks=render_checks(all_checks),
            blocked=blocked,
            warnings=[
                "A bulk write can come back HTTP 200 with some items rejected. "
                "apply_keyword_bids_bulk reports per-item results rather than a "
                "single success."
            ],
            note="One out-of-bounds item blocks the whole batch.",
        )

    @mcp.tool(
        title="Apply several keyword bid changes (CHANGES WHAT THESE KEYWORDS COST)",
        annotations=APPLY,
    )
    def apply_keyword_bids_bulk(
        preview_token: Annotated[
            str, Field(description="The token returned by preview_keyword_bids_bulk")
        ],
    ) -> BulkApplyResult:
        """Write the batch the matching preview described. Live account.

        HTTP 200 from a bulk endpoint does NOT mean every item applied: Apple
        returns a per-item success/error alongside the correlationId we set. Each
        item is reported individually and a mixed result is recorded as
        `partial`, never as a bare success.
        """
        pending = state.previews.take(preview_token, "apply_keyword_bids_bulk")
        begin_apply(pending, pending.projected_delta)

        # Re-read every keyword before writing any of them: a partial TOCTOU
        # check would be worse than none, because it would look like a check.
        for item in pending.items:
            keyword = client.get_keyword(item["keyword_id"])
            current = money_amount(keyword.bid)
            confirm_unchanged(
                str(current) if current is not None else "none",
                item["before"] if item["before"] is not None else "none",
                f"keyword {item['keyword_id']} bid",
            )

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id,
            tool="apply_keyword_bids_bulk",
            entity_type="keyword",
            entity_id=",".join(str(i["keyword_id"]) for i in pending.items),
            entity_name=f"{len(pending.items)} keywords",
            path="; ".join(i["path"] for i in pending.items),
            field="bid",
            before=", ".join(str(i["before"]) for i in pending.items),
            after=", ".join(i["after"] for i in pending.items),
            campaign_id=None,
            projected_daily_spend_delta=str(pending.projected_delta),
            extra={"items": pending.items},
        )

        # correlationId is always set, so a per-item result can be tied back to
        # the request item it came from. Without it a partial failure is just a
        # list of errors with no way to say WHICH keyword did not change.
        request = KeywordUpdateBulkRequest(
            allow_partial_success=True,
            items=[
                KeywordUpdateBulkRequestItem(
                    correlation_id=index,
                    data=BulkKeywordUpdate(
                        id=item["keyword_id"],
                        bid=to_money(item["after"], item["currency"]),
                    ),
                )
                for index, item in enumerate(pending.items)
            ],
        )
        response = run_write(
            entry_id,
            "keywords_bulk_update_post",
            x_ap_context=context_header(),
            keyword_update_bulk_request=request,
        )
        results = unwrap(response, "bulk keyword update") or []

        by_correlation = {r.correlation_id: r for r in results}
        lines, failures = [], []
        succeeded = 0
        for index, item in enumerate(pending.items):
            result = by_correlation.get(index)
            if result is None:
                failures.append(f"keyword {item['keyword_id']}: Apple returned no result for it")
                lines.append(f"{item['keyword_id']} {item['text']!r}: NO RESULT RETURNED")
                continue
            if result.success:
                succeeded += 1
                lines.append(
                    f"{item['keyword_id']} {item['text']!r}: {item['before']} -> "
                    f"{format_money(getattr(result.result, 'bid', None))}"
                )
            else:
                failures.append(f"keyword {item['keyword_id']}: {result.error}")
                lines.append(f"{item['keyword_id']} {item['text']!r}: FAILED -- {result.error}")

        failed = len(pending.items) - succeeded
        outcome = (
            ledger.APPLIED if failed == 0 else (ledger.FAILED if succeeded == 0 else ledger.PARTIAL)
        )
        ledger.write_outcome(
            entry_id,
            outcome=outcome,
            detail=f"{succeeded} applied, {failed} failed",
            failures=[{"message": f} for f in failures],
        )
        if succeeded:
            state.counters.record(pending.projected_delta)

        return BulkApplyResult(
            applied=succeeded > 0,
            entry_id=entry_id,
            tool="apply_keyword_bids_bulk",
            succeeded=succeeded,
            failed=failed,
            results=lines,
            failures=failures,
            projected_daily_spend_delta=str(pending.projected_delta),
            session={k: str(v) for k, v in state.counters.snapshot().items()},
            note=(
                f"Recorded as {outcome}. "
                + (
                    "Some items did NOT apply -- read the failures list before "
                    "assuming the batch landed."
                    if failed
                    else f"Undo with revert_change(entry_id='{entry_id}')."
                )
            ),
        )

    # --- keyword status ------------------------------------------------------

    @mcp.tool(title="Preview pausing or enabling a keyword (no write)", annotations=PREVIEW)
    def preview_keyword_status(
        keyword_id: Annotated[int, Field(description="Apple keyword id")],
        status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> ChangePreview:
        """Show what pausing or enabling one keyword would do. Writes nothing."""
        target = _status(status, {"ENABLED", "PAUSED"})
        keyword = client.get_keyword(keyword_id)
        ad_group, campaign = keyword_context(keyword)
        before = enum_value(keyword.status)
        if before == target:
            raise ToolError(f"keyword {keyword_id} is already {target}; nothing to change.")

        spend, taps, impressions, installs, metrics_error = entity_metrics_7d(
            "apps_keyword_reports", "keywordId", keyword_id, keyword.campaign_id
        )
        projected = project_pause(spend) if target == "PAUSED" else Decimal("0")
        checks = [
            check_campaign_in_scope(state.limits, keyword.campaign_id),
            check_known_status("keyword_status_known", before),
        ]
        entity_serving = enum_value(keyword.display_status)
        warnings = serving_warning(entity_serving, "keyword") + metrics_warning(metrics_error)
        if target == "ENABLED":
            warnings.append(
                "Enabling a keyword RESUMES spend on it. The projection shows 0 "
                "because past spend while paused is not a guide to future spend."
            )

        return build_preview(
            tool="apply_keyword_status",
            entity_type="keyword",
            entity_id=keyword_id,
            entity_name=keyword.text,
            path=entity_path(campaign, ad_group, keyword.text),
            field="status",
            before=before or "unknown",
            after=target,
            before_num=None,
            after_num=None,
            campaign=campaign,
            entity_serving=entity_serving,
            metrics=metrics_block(spend, taps, impressions, installs),
            projected=projected,
            checks=checks,
            warnings=warnings,
            note="Reversible: apply the opposite status to undo, or use revert_change.",
            item={
                "keyword_id": keyword_id,
                "before": before,
                "after": target,
                "text": keyword.text,
                "campaign_id": keyword.campaign_id,
                "path": entity_path(campaign, ad_group, keyword.text),
            },
        )

    @mcp.tool(title="Apply a keyword pause or enable (CHANGES LIVE DELIVERY)", annotations=APPLY)
    def apply_keyword_status(
        preview_token: Annotated[
            str, Field(description="The token returned by preview_keyword_status")
        ],
    ) -> ApplyResult:
        """Pause or enable the keyword the matching preview described. Live account."""
        pending = state.previews.take(preview_token, "apply_keyword_status")
        item = pending.items[0]
        begin_apply(pending, pending.projected_delta)

        keyword = client.get_keyword(item["keyword_id"])
        confirm_unchanged(
            enum_value(keyword.status) or "unknown",
            item["before"] or "unknown",
            f"keyword {item['keyword_id']} status",
        )

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id,
            tool="apply_keyword_status",
            entity_type="keyword",
            entity_id=item["keyword_id"],
            entity_name=item["text"],
            path=item["path"],
            field="status",
            before=item["before"],
            after=item["after"],
            campaign_id=item["campaign_id"],
            projected_daily_spend_delta=str(pending.projected_delta),
        )
        response = run_write(
            entry_id,
            "keywords_id_put",
            id=str(item["keyword_id"]),
            x_ap_context=context_header(),
            keyword_update=KeywordUpdate(status=KeywordStatus(item["after"])),
        )
        updated = unwrap(response, "keyword update")
        return finish(
            entry_id,
            tool="apply_keyword_status",
            entity_type="keyword",
            entity_id=item["keyword_id"],
            entity_name=item["text"],
            path=item["path"],
            field="status",
            before=item["before"] or "unknown",
            after=item["after"],
            observed_after=enum_value(getattr(updated, "status", None)) if updated else None,
            projected=pending.projected_delta,
        )

    # --- ad group status -----------------------------------------------------

    @mcp.tool(title="Preview pausing or enabling an ad group (no write)", annotations=PREVIEW)
    def preview_ad_group_status(
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> ChangePreview:
        """Show what pausing or enabling an ad group would do. Writes nothing.

        An ad group carries every keyword under it, so this is a much wider
        change than pausing one keyword. The 7-day figures are the ad group's
        total, which is the number that matters.
        """
        target = _status(status, {"ENABLED", "PAUSED"})
        ad_group = client.get_ad_group(ad_group_id)
        campaign = client.get_campaign(ad_group.campaign_id) if ad_group.campaign_id else None
        before = enum_value(ad_group.status)
        if before == target:
            raise ToolError(f"ad group {ad_group_id} is already {target}; nothing to change.")

        spend, taps, impressions, installs, metrics_error = entity_metrics_7d(
            "apps_ad_group_reports", "adGroupId", ad_group_id, ad_group.campaign_id
        )
        projected = project_pause(spend) if target == "PAUSED" else Decimal("0")
        checks = [
            check_campaign_in_scope(state.limits, ad_group.campaign_id),
            check_known_status("ad_group_status_known", before),
        ]
        entity_serving = enum_value(ad_group.display_status)
        warnings = serving_warning(entity_serving, "ad group") + metrics_warning(metrics_error)
        warnings.append(
            "This affects EVERY keyword in the ad group, not one of them."
        )

        return build_preview(
            tool="apply_ad_group_status",
            entity_type="ad_group",
            entity_id=ad_group_id,
            entity_name=ad_group.name,
            path=entity_path(campaign, ad_group),
            field="status",
            before=before or "unknown",
            after=target,
            before_num=None,
            after_num=None,
            campaign=campaign,
            entity_serving=entity_serving,
            metrics=metrics_block(spend, taps, impressions, installs),
            projected=projected,
            checks=checks,
            warnings=warnings,
            note="Reversible: apply the opposite status to undo, or use revert_change.",
            item={
                "ad_group_id": ad_group_id,
                "before": before,
                "after": target,
                "name": ad_group.name,
                "campaign_id": ad_group.campaign_id,
                "path": entity_path(campaign, ad_group),
            },
        )

    @mcp.tool(
        title="Apply an ad group pause or enable (AFFECTS EVERY KEYWORD IN IT)",
        annotations=APPLY,
    )
    def apply_ad_group_status(
        preview_token: Annotated[
            str, Field(description="The token returned by preview_ad_group_status")
        ],
    ) -> ApplyResult:
        """Pause or enable the ad group the matching preview described. Live account."""
        pending = state.previews.take(preview_token, "apply_ad_group_status")
        item = pending.items[0]
        begin_apply(pending, pending.projected_delta)

        ad_group = client.get_ad_group(item["ad_group_id"])
        confirm_unchanged(
            enum_value(ad_group.status) or "unknown",
            item["before"] or "unknown",
            f"ad group {item['ad_group_id']} status",
        )

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id,
            tool="apply_ad_group_status",
            entity_type="ad_group",
            entity_id=item["ad_group_id"],
            entity_name=item["name"],
            path=item["path"],
            field="status",
            before=item["before"],
            after=item["after"],
            campaign_id=item["campaign_id"],
            projected_daily_spend_delta=str(pending.projected_delta),
        )
        response = run_write(
            entry_id,
            "adgroups_id_put",
            id=str(item["ad_group_id"]),
            x_ap_context=context_header(),
            ad_group_update=AdGroupUpdate(status=AdGroupStatus(item["after"])),
        )
        updated = unwrap(response, "ad group update")
        return finish(
            entry_id,
            tool="apply_ad_group_status",
            entity_type="ad_group",
            entity_id=item["ad_group_id"],
            entity_name=item["name"],
            path=item["path"],
            field="status",
            before=item["before"] or "unknown",
            after=item["after"],
            observed_after=enum_value(getattr(updated, "status", None)) if updated else None,
            projected=pending.projected_delta,
        )

    # --- ad group default bid ------------------------------------------------

    @mcp.tool(title="Preview an ad group default bid change (no write)", annotations=PREVIEW)
    def preview_ad_group_default_bid(
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        new_bid: Annotated[
            str,
            Field(
                description='New default bid as a decimal string in major units, e.g. "3.00"'
            ),
        ],
    ) -> ChangePreview:
        """Show what changing an ad group's default bid would do. Writes nothing.

        The default bid is what every keyword in the ad group pays unless it
        carries a bid of its own -- and in a Search Tab or Search Match ad group,
        which has no keywords at all, it is the ONLY bid. The blast radius is
        therefore wider than a keyword bid, so the warnings say how many keywords
        this actually reprices and how many override it.
        """
        after_num = parse_decimal(new_bid, "new_bid")
        ad_group = client.get_ad_group(ad_group_id)
        campaign = client.get_campaign(ad_group.campaign_id) if ad_group.campaign_id else None
        current_bid = _ad_group_bid(ad_group)
        before_num = money_amount(current_bid)
        # An ad group with no bid set yet has no currency to borrow, so fall back
        # to the campaign's budget currency rather than sending a null one.
        currency = money_currency(current_bid) or _campaign_currency(campaign)

        spend, taps, impressions, installs, metrics_error = entity_metrics_7d(
            "apps_ad_group_reports", "adGroupId", ad_group_id, ad_group.campaign_id
        )
        projected = project_bid_change(taps, before_num, after_num)

        checks = [
            check_campaign_in_scope(state.limits, ad_group.campaign_id),
            check_currency(state.limits, currency),
            check_ceiling("max_bid", after_num, state.limits.max_bid, currency or ""),
            check_pct_increase(
                "max_bid_increase_pct", before_num, after_num, state.limits.max_bid_increase_pct
            ),
            check_known_status("ad_group_status_known", enum_value(ad_group.status)),
            check_bid_settable(_bid_strategy_type(ad_group), "ad group default bid"),
        ]
        entity_serving = enum_value(ad_group.display_status)
        warnings = serving_warning(entity_serving, "ad group") + metrics_warning(metrics_error)
        # No "the ad group is paused" line here: serving_warning() already says it
        # from display_status. The keyword tools add one because a keyword's own
        # status is genuinely separate from its ad group's serving status; an ad
        # group's is not, so a second line would be the same fact twice.
        warnings.extend(_default_bid_blast_radius(ad_group_id))
        if before_num is None:
            warnings.append(
                "This ad group reports no default bid today, so there is no previous "
                "value to put back and revert_change will refuse this entry. Note the "
                "current state somewhere before approving."
            )

        return build_preview(
            tool="apply_ad_group_default_bid",
            entity_type="ad_group",
            entity_id=ad_group_id,
            entity_name=ad_group.name,
            path=entity_path(campaign, ad_group),
            field="default_bid",
            before=format_money(current_bid),
            after=f"{after_num} {currency or ''}".strip(),
            before_num=before_num,
            after_num=after_num,
            campaign=campaign,
            entity_serving=entity_serving,
            metrics=metrics_block(spend, taps, impressions, installs),
            projected=projected,
            checks=checks,
            warnings=warnings,
            note=(
                "The default bid lives at bidStrategy.bid; the write sends this ad "
                "group's existing bidStrategyType and bidStrategyGoal back unchanged "
                "alongside it, so the bidding model does not move. "
                "projected_daily_spend_delta is an UPPER BOUND: it assumes tap volume "
                "is unchanged, which a bid change is precisely intended to alter. Use "
                "it to catch a 100x typo, not as a forecast."
            ),
            item={
                "ad_group_id": ad_group_id,
                "before": str(before_num) if before_num is not None else None,
                "after": str(after_num),
                "currency": currency,
                "name": ad_group.name,
                "campaign_id": ad_group.campaign_id,
                "path": entity_path(campaign, ad_group),
                "bid_strategy_type": _bid_strategy_type(ad_group),
                "bid_strategy_goal": _bid_strategy_goal(ad_group),
            },
        )

    @mcp.tool(
        title="Apply an ad group default bid change (CHANGES WHAT THIS AD GROUP COSTS)",
        annotations=APPLY,
    )
    def apply_ad_group_default_bid(
        preview_token: Annotated[
            str, Field(description="The token returned by preview_ad_group_default_bid")
        ],
    ) -> ApplyResult:
        """Write the ad group default bid the matching preview described. Live account."""
        pending = state.previews.take(preview_token, "apply_ad_group_default_bid")
        item = pending.items[0]
        begin_apply(pending, pending.projected_delta)

        ad_group = client.get_ad_group(item["ad_group_id"])
        current = money_amount(_ad_group_bid(ad_group))
        confirm_unchanged(
            str(current) if current is not None else "none",
            item["before"] if item["before"] is not None else "none",
            f"ad group {item['ad_group_id']} default bid",
        )

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id,
            tool="apply_ad_group_default_bid",
            entity_type="ad_group",
            entity_id=item["ad_group_id"],
            entity_name=item["name"],
            path=item["path"],
            field="default_bid",
            before=item["before"],
            after=item["after"],
            campaign_id=item["campaign_id"],
            projected_daily_spend_delta=str(pending.projected_delta),
        )
        response = run_write(
            entry_id,
            "adgroups_id_put",
            id=str(item["ad_group_id"]),
            x_ap_context=context_header(),
            ad_group_update=AdGroupUpdate(
                bid_strategy=_bid_strategy_update(
                    item["after"],
                    item["currency"],
                    item.get("bid_strategy_type"),
                    item.get("bid_strategy_goal"),
                )
            ),
        )
        updated = unwrap(response, "ad group update")
        return finish(
            entry_id,
            tool="apply_ad_group_default_bid",
            entity_type="ad_group",
            entity_id=item["ad_group_id"],
            entity_name=item["name"],
            path=item["path"],
            field="default_bid",
            before=item["before"] or "none",
            after=item["after"],
            observed_after=format_money(_ad_group_bid(updated)) if updated else None,
            projected=pending.projected_delta,
            failure_note=(
                "NOT APPLIED. Apple returned success and left the default bid alone. "
                "That usually means the ad group is on an automated bid strategy "
                "(MAX_CONVERSIONS / MAX_ENGAGEMENTS), where Apple sets the bids and a "
                "written one is accepted and ignored. Recorded as failed, so "
                "revert_change will refuse it -- there is nothing to undo."
            ),
        )

    # --- campaign status -----------------------------------------------------

    @mcp.tool(title="Preview pausing or enabling a WHOLE CAMPAIGN (no write)", annotations=PREVIEW)
    def preview_campaign_status(
        campaign_id: Annotated[int, Field(description="Apple campaign id")],
        status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> ChangePreview:
        """Show what pausing or enabling an entire campaign would do. Writes nothing.

        The widest change this server can make. Everything under the campaign
        stops or starts.
        """
        target = _status(status, {"ENABLED", "PAUSED"})
        campaign = client.get_campaign(campaign_id)
        before = enum_value(campaign.status)
        if before == target:
            raise ToolError(f"campaign {campaign_id} is already {target}; nothing to change.")

        spend, taps, impressions, installs, metrics_error = entity_metrics_7d(
            "apps_campaign_reports", "campaignId", campaign_id
        )
        projected = project_pause(spend) if target == "PAUSED" else Decimal("0")
        checks = [
            check_campaign_in_scope(state.limits, campaign_id),
            check_known_status("campaign_status_known", before),
        ]
        entity_serving = enum_value(campaign.display_status)
        warnings = serving_warning(entity_serving, "campaign") + metrics_warning(metrics_error)
        warnings.append("This stops or starts EVERY ad group and keyword in the campaign.")

        return build_preview(
            tool="apply_campaign_status",
            entity_type="campaign",
            entity_id=campaign_id,
            entity_name=campaign.name,
            path=entity_path(campaign),
            field="status",
            before=before or "unknown",
            after=target,
            before_num=None,
            after_num=None,
            campaign=campaign,
            entity_serving=entity_serving,
            metrics=metrics_block(spend, taps, impressions, installs),
            projected=projected,
            checks=checks,
            warnings=warnings,
            note=(
                "Reversible by re-enabling, but a paused campaign loses delivery "
                "for as long as it is paused, and that time is not recoverable."
            ),
            item={
                "campaign_id": campaign_id,
                "before": before,
                "after": target,
                "name": campaign.name,
                "path": entity_path(campaign),
            },
        )

    @mcp.tool(
        title="Apply a campaign pause or enable (STOPS OR STARTS A WHOLE CAMPAIGN)",
        annotations=APPLY_DESTRUCTIVE,
    )
    def apply_campaign_status(
        preview_token: Annotated[
            str, Field(description="The token returned by preview_campaign_status")
        ],
    ) -> ApplyResult:
        """Pause or enable the whole campaign the matching preview described."""
        pending = state.previews.take(preview_token, "apply_campaign_status")
        item = pending.items[0]
        begin_apply(pending, pending.projected_delta)

        campaign = client.get_campaign(item["campaign_id"])
        confirm_unchanged(
            enum_value(campaign.status) or "unknown",
            item["before"] or "unknown",
            f"campaign {item['campaign_id']} status",
        )

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id,
            tool="apply_campaign_status",
            entity_type="campaign",
            entity_id=item["campaign_id"],
            entity_name=item["name"],
            path=item["path"],
            field="status",
            before=item["before"],
            after=item["after"],
            campaign_id=item["campaign_id"],
            projected_daily_spend_delta=str(pending.projected_delta),
        )
        response = run_write(
            entry_id,
            "campaigns_id_put",
            id=str(item["campaign_id"]),
            x_ap_context=context_header(),
            campaign_update=CampaignUpdate(status=CampaignStatus(item["after"])),
        )
        updated = unwrap(response, "campaign update")
        return finish(
            entry_id,
            tool="apply_campaign_status",
            entity_type="campaign",
            entity_id=item["campaign_id"],
            entity_name=item["name"],
            path=item["path"],
            field="status",
            before=item["before"] or "unknown",
            after=item["after"],
            observed_after=enum_value(getattr(updated, "status", None)) if updated else None,
            projected=pending.projected_delta,
        )

    # --- campaign daily budget -----------------------------------------------

    @mcp.tool(title="Preview a campaign daily budget change (no write)", annotations=PREVIEW)
    def preview_campaign_daily_budget(
        campaign_id: Annotated[int, Field(description="Apple campaign id")],
        new_daily_budget: Annotated[
            str, Field(description='New daily budget as a decimal string, e.g. "120.00"')
        ],
    ) -> ChangePreview:
        """Show what changing a campaign's daily budget would do. Writes nothing.

        The budget delta is EXACT, not an estimate: it is the spend cap itself
        moving, not a guess about behaviour.
        """
        after_num = parse_decimal(new_daily_budget, "new_daily_budget")
        campaign = client.get_campaign(campaign_id)
        current = campaign.daily_budget.value if campaign.daily_budget else None
        before_num = money_amount(current)
        currency = money_currency(current)

        spend, taps, impressions, installs, metrics_error = entity_metrics_7d(
            "apps_campaign_reports", "campaignId", campaign_id
        )
        projected = project_budget_change(before_num, after_num)
        checks = [
            check_campaign_in_scope(state.limits, campaign_id),
            check_currency(state.limits, currency),
            check_ceiling(
                "max_daily_budget", after_num, state.limits.max_daily_budget, currency or ""
            ),
            check_pct_increase(
                "max_budget_change_pct",
                before_num,
                after_num,
                state.limits.max_budget_change_pct,
            ),
            check_known_status("campaign_status_known", enum_value(campaign.status)),
        ]
        entity_serving = enum_value(campaign.display_status)
        warnings = serving_warning(entity_serving, "campaign") + metrics_warning(metrics_error)
        if before_num is not None and after_num < before_num:
            warnings.append(
                "Lowering a daily budget can stop delivery part-way through today "
                "if the campaign has already spent more than the new cap."
            )

        return build_preview(
            tool="apply_campaign_daily_budget",
            entity_type="campaign",
            entity_id=campaign_id,
            entity_name=campaign.name,
            path=entity_path(campaign),
            field="daily_budget",
            before=format_money(current),
            after=f"{after_num} {currency or ''}".strip(),
            before_num=before_num,
            after_num=after_num,
            campaign=campaign,
            entity_serving=entity_serving,
            metrics=metrics_block(spend, taps, impressions, installs),
            projected=projected,
            checks=checks,
            warnings=warnings,
            note=(
                "projected_daily_spend_delta here is the exact change in the cap. "
                "Actual spend depends on whether the campaign was hitting the old "
                "cap at all -- compare it with the last 7 days' spend."
            ),
            item={
                "campaign_id": campaign_id,
                "before": str(before_num) if before_num is not None else None,
                "after": str(after_num),
                "currency": currency,
                "name": campaign.name,
                "path": entity_path(campaign),
            },
        )

    @mcp.tool(
        title="Apply a campaign daily budget change (SPENDS MONEY)",
        annotations=APPLY_DESTRUCTIVE,
    )
    def apply_campaign_daily_budget(
        preview_token: Annotated[
            str, Field(description="The token returned by preview_campaign_daily_budget")
        ],
    ) -> ApplyResult:
        """Write the daily budget the matching preview described. Live account."""
        pending = state.previews.take(preview_token, "apply_campaign_daily_budget")
        item = pending.items[0]
        begin_apply(pending, pending.projected_delta)

        campaign = client.get_campaign(item["campaign_id"])
        current = campaign.daily_budget.value if campaign.daily_budget else None
        current_amount = money_amount(current)
        confirm_unchanged(
            str(current_amount) if current_amount is not None else "none",
            item["before"] if item["before"] is not None else "none",
            f"campaign {item['campaign_id']} daily budget",
        )

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id,
            tool="apply_campaign_daily_budget",
            entity_type="campaign",
            entity_id=item["campaign_id"],
            entity_name=item["name"],
            path=item["path"],
            field="daily_budget",
            before=item["before"],
            after=item["after"],
            campaign_id=item["campaign_id"],
            projected_daily_spend_delta=str(pending.projected_delta),
        )
        response = run_write(
            entry_id,
            "campaigns_id_put",
            id=str(item["campaign_id"]),
            x_ap_context=context_header(),
            campaign_update=CampaignUpdate(
                daily_budget=DailyBudgetUpdate(value=to_money(item["after"], item["currency"]))
            ),
        )
        updated = unwrap(response, "campaign update")
        observed = updated.daily_budget.value if updated and updated.daily_budget else None
        return finish(
            entry_id,
            tool="apply_campaign_daily_budget",
            entity_type="campaign",
            entity_id=item["campaign_id"],
            entity_name=item["name"],
            path=item["path"],
            field="daily_budget",
            before=item["before"] or "none",
            after=item["after"],
            observed_after=format_money(observed) if observed else None,
            projected=pending.projected_delta,
        )

    # --- negative keywords ---------------------------------------------------

    @mcp.tool(title="Preview adding negative keywords (no write)", annotations=PREVIEW)
    def preview_negative_keywords_add(
        texts: Annotated[list[str], Field(description="Search terms to exclude")],
        match_type: Annotated[str, Field(description="EXACT or BROAD")] = "EXACT",
        campaign_id: Annotated[
            int | None, Field(description="Add at campaign level; omit if using ad_group_id")
        ] = None,
        ad_group_id: Annotated[
            int | None, Field(description="Add at ad group level; omit if using campaign_id")
        ] = None,
    ) -> BulkChangePreview:
        """Show what adding these negative keywords would do. Writes nothing.

        A negative keyword stops matching traffic, so its effect on spend is
        negative or zero -- but it is also the one change here that is NOT
        idempotent: running it twice creates two of each. Pause rather than
        delete if you need to undo it.
        """
        target_match = _status(match_type, {"EXACT", "BROAD"}, "match_type")
        cleaned = [t.strip() for t in texts if t and t.strip()]
        if not cleaned:
            raise ToolError("texts is empty; there is nothing to add.")
        if (campaign_id is None) == (ad_group_id is None):
            raise ToolError(
                "give exactly one of campaign_id or ad_group_id. A campaign-level "
                "negative applies to every ad group in it; an ad-group-level one "
                "applies only to that ad group."
            )

        if ad_group_id is not None:
            ad_group = client.get_ad_group(ad_group_id)
            resolved_campaign_id = ad_group.campaign_id
            campaign = client.get_campaign(resolved_campaign_id) if resolved_campaign_id else None
            scope = entity_path(campaign, ad_group)
        else:
            ad_group = None
            resolved_campaign_id = campaign_id
            campaign = client.get_campaign(campaign_id)
            scope = entity_path(campaign)

        existing = _existing_negatives(resolved_campaign_id, ad_group_id)
        duplicates = [t for t in cleaned if (t.lower(), target_match) in existing]

        checks = [
            check_count(state.limits, len(cleaned)),
            check_campaign_in_scope(state.limits, resolved_campaign_id),
        ]
        changes = [
            ChangePreview(
                preview_token="(covered by the batch token)",
                tool="apply_negative_keywords_add",
                entity_type="negative_keyword",
                entity_id="(new)",
                entity_name=text,
                path=f"{scope} > NegativeKeyword {text!r}",
                field="exists",
                before="absent",
                after=f"{target_match} negative",
                campaign_id=str(resolved_campaign_id) if resolved_campaign_id else None,
                campaign_name=getattr(campaign, "name", None),
                campaign_daily_budget=campaign_facts(campaign)[1],
                campaign_serving_status=enum_value(getattr(campaign, "display_status", None)),
                entity_serving_status=enum_value(getattr(ad_group, "display_status", None))
                if ad_group is not None
                else enum_value(getattr(campaign, "display_status", None)),
                projected_daily_spend_delta="0",
                guardrail_checks=[],
                blocked=False,
                warnings=["already present -- adding it again creates a duplicate"]
                if (text.lower(), target_match) in existing
                else [],
            )
            for text in cleaned
        ]
        checks.append(check_projected_delta(state.limits, Decimal("0")))
        blocked = any_failed(checks)
        items = [
            {
                "text": text,
                "match_type": target_match,
                "campaign_id": resolved_campaign_id,
                "ad_group_id": ad_group_id,
                "path": f"{scope} > NegativeKeyword {text!r}",
            }
            for text in cleaned
        ]
        token = state.previews.mint(
            "apply_negative_keywords_add", items, Decimal("0"), blocked
        )
        warnings = serving_warning(
            enum_value(getattr(ad_group or campaign, "display_status", None)),
            "ad group" if ad_group is not None else "campaign",
        )
        warnings.append(
            "NOT idempotent: applying this twice creates duplicate negatives."
        )
        if duplicates:
            warnings.append(
                f"{len(duplicates)} of these already exist as {target_match} negatives: "
                + ", ".join(repr(d) for d in duplicates[:10])
            )
        return BulkChangePreview(
            preview_token=token,
            tool="apply_negative_keywords_add",
            entity_type="negative_keyword",
            count=len(items),
            changes=changes,
            total_projected_daily_spend_delta="0",
            guardrail_checks=render_checks(checks),
            blocked=blocked,
            warnings=warnings,
            note=(
                "Adding a negative can only reduce spend, so the projection is 0 "
                "rather than an estimate of the saving. To undo, PAUSE the "
                "negative -- deletion is not exposed."
            ),
        )

    @mcp.tool(
        title="Apply negative keywords (EXCLUDES TRAFFIC; not idempotent)",
        annotations=APPLY,
    )
    def apply_negative_keywords_add(
        preview_token: Annotated[
            str, Field(description="The token returned by preview_negative_keywords_add")
        ],
    ) -> BulkApplyResult:
        """Create the negative keywords the matching preview described. Live account.

        Not idempotent, which is why the token is single-use: a retried call
        would create a second copy of every negative.
        """
        pending = state.previews.take(preview_token, "apply_negative_keywords_add")
        begin_apply(pending, Decimal("0"))

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id,
            tool="apply_negative_keywords_add",
            entity_type="negative_keyword",
            entity_id="(new)",
            entity_name=f"{len(pending.items)} negative keywords",
            path="; ".join(i["path"] for i in pending.items),
            field="exists",
            before="absent",
            after=", ".join(f"{i['text']} ({i['match_type']})" for i in pending.items),
            campaign_id=pending.items[0]["campaign_id"],
            projected_daily_spend_delta="0",
            extra={"items": pending.items},
        )
        request = NegativeKeywordCreateBulkRequest(
            allow_partial_success=True,
            items=[
                NegativeKeywordCreateBulkRequestItem(
                    correlation_id=index,
                    data=BulkNegativeKeywordCreate(
                        campaign_id=item["campaign_id"],
                        ad_group_id=item["ad_group_id"],
                        text=item["text"],
                        match_type=item["match_type"],
                        status=NegativeKeywordStatus("ENABLED"),
                    ),
                )
                for index, item in enumerate(pending.items)
            ],
        )
        response = run_write(
            entry_id,
            "negative_keywords_bulk_create_post",
            x_ap_context=context_header(),
            negative_keyword_create_bulk_request=request,
        )
        results = unwrap(response, "bulk negative keyword create") or []

        by_correlation = {r.correlation_id: r for r in results}
        lines, failures = [], []
        succeeded = 0
        created_ids = []
        for index, item in enumerate(pending.items):
            result = by_correlation.get(index)
            if result is None:
                failures.append(f"{item['text']!r}: Apple returned no result for it")
                lines.append(f"{item['text']!r}: NO RESULT RETURNED")
            elif result.success:
                succeeded += 1
                new_id = getattr(result.result, "id", None)
                created_ids.append(new_id)
                lines.append(f"{item['text']!r} ({item['match_type']}): created as {new_id}")
            else:
                failures.append(f"{item['text']!r}: {result.error}")
                lines.append(f"{item['text']!r}: FAILED -- {result.error}")

        failed = len(pending.items) - succeeded
        outcome = (
            ledger.APPLIED if failed == 0 else (ledger.FAILED if succeeded == 0 else ledger.PARTIAL)
        )
        ledger.write_outcome(
            entry_id,
            outcome=outcome,
            detail=f"{succeeded} created, {failed} failed",
            observed_after=[str(i) for i in created_ids],
            failures=[{"message": f} for f in failures],
        )
        if succeeded:
            state.counters.record(Decimal("0"))

        return BulkApplyResult(
            applied=succeeded > 0,
            entry_id=entry_id,
            tool="apply_negative_keywords_add",
            succeeded=succeeded,
            failed=failed,
            results=lines,
            failures=failures,
            projected_daily_spend_delta="0",
            session={k: str(v) for k, v in state.counters.snapshot().items()},
            note=(
                f"Recorded as {outcome}. To undo, pause each one with "
                f"preview_negative_keyword_pause -- revert_change cannot delete "
                f"them, because deletion is not exposed."
            ),
        )

    @mcp.tool(title="Preview pausing or enabling a negative keyword (no write)", annotations=PREVIEW)
    def preview_negative_keyword_pause(
        negative_keyword_id: Annotated[int, Field(description="Apple negative keyword id")],
        status: Annotated[str, Field(description="PAUSED to stop excluding, ENABLED to resume")] = "PAUSED",
    ) -> ChangePreview:
        """Show what pausing or enabling a negative keyword would do. Writes nothing.

        Pausing a negative keyword lets its traffic back in, so it can INCREASE
        spend. That is the reverse of what "pause" usually implies here.
        """
        target = _status(status, {"ENABLED", "PAUSED"})
        negative = client.get_negative_keyword(negative_keyword_id)
        before = enum_value(negative.status)
        if before == target:
            raise ToolError(
                f"negative keyword {negative_keyword_id} is already {target}; nothing to change."
            )
        campaign = client.get_campaign(negative.campaign_id) if negative.campaign_id else None
        ad_group = client.get_ad_group(negative.ad_group_id) if negative.ad_group_id else None
        path = entity_path(campaign, ad_group, negative.text, keyword_label="NegativeKeyword")

        checks = [
            check_campaign_in_scope(state.limits, negative.campaign_id),
            check_known_status("negative_keyword_status_known", before),
        ]
        entity_serving = enum_value(getattr(campaign, "display_status", None))
        warnings = serving_warning(entity_serving, "campaign")
        if target == "PAUSED":
            warnings.append(
                "Pausing a NEGATIVE keyword stops excluding this term, so traffic "
                "and spend can go UP, not down."
            )

        return build_preview(
            tool="apply_negative_keyword_pause",
            entity_type="negative_keyword",
            entity_id=negative_keyword_id,
            entity_name=negative.text,
            path=path,
            field="status",
            before=before or "unknown",
            after=target,
            before_num=None,
            after_num=None,
            campaign=campaign,
            entity_serving=entity_serving,
            metrics=None,
            projected=Decimal("0"),
            checks=checks,
            warnings=warnings,
            note=(
                "Pausing is the supported way to undo a negative keyword. Deletion "
                "is deliberately not exposed -- it would be the only irreversible "
                "operation here."
            ),
            item={
                "negative_keyword_id": negative_keyword_id,
                "before": before,
                "after": target,
                "text": negative.text,
                "campaign_id": negative.campaign_id,
                "path": path,
            },
        )

    @mcp.tool(
        title="Apply a negative keyword pause or enable (CHANGES WHAT TRAFFIC IS EXCLUDED)",
        annotations=APPLY,
    )
    def apply_negative_keyword_pause(
        preview_token: Annotated[
            str, Field(description="The token returned by preview_negative_keyword_pause")
        ],
    ) -> ApplyResult:
        """Pause or enable the negative keyword the matching preview described."""
        pending = state.previews.take(preview_token, "apply_negative_keyword_pause")
        item = pending.items[0]
        begin_apply(pending, Decimal("0"))

        negative = client.get_negative_keyword(item["negative_keyword_id"])
        confirm_unchanged(
            enum_value(negative.status) or "unknown",
            item["before"] or "unknown",
            f"negative keyword {item['negative_keyword_id']} status",
        )

        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id,
            tool="apply_negative_keyword_pause",
            entity_type="negative_keyword",
            entity_id=item["negative_keyword_id"],
            entity_name=item["text"],
            path=item["path"],
            field="status",
            before=item["before"],
            after=item["after"],
            campaign_id=item["campaign_id"],
            projected_daily_spend_delta="0",
        )
        response = run_write(
            entry_id,
            "negative_keywords_id_put",
            id=str(item["negative_keyword_id"]),
            x_ap_context=context_header(),
            negative_keyword_update=NegativeKeywordUpdate(
                status=NegativeKeywordStatus(item["after"])
            ),
        )
        updated = unwrap(response, "negative keyword update")
        return finish(
            entry_id,
            tool="apply_negative_keyword_pause",
            entity_type="negative_keyword",
            entity_id=item["negative_keyword_id"],
            entity_name=item["text"],
            path=item["path"],
            field="status",
            before=item["before"] or "unknown",
            after=item["after"],
            observed_after=enum_value(getattr(updated, "status", None)) if updated else None,
            projected=Decimal("0"),
        )

    # --- revert --------------------------------------------------------------

    @mcp.tool(
        title="Undo one change from the ledger (writes the old value back)",
        annotations=APPLY,
    )
    def revert_change(
        entry_id: Annotated[
            str, Field(description="entry_id from list_my_changes or from an apply result")
        ],
    ) -> ApplyResult:
        """Put a single previous change back the way it was.

        Refuses unless that entry is recorded as `applied`, and re-reads the
        entity first: if its current value is no longer what the entry said it
        was left at, somebody else has moved it since and reverting would silently
        overwrite THEIR change. Writes a new ledger entry rather than erasing the
        old one -- the ledger is append-only.

        Cannot undo apply_negative_keywords_add: that created rows, and deletion
        is not exposed. Pause them instead.
        """
        guard_writes()
        entry = ledger.find_entry(entry_id)
        if entry is None:
            raise ToolError(f"no ledger entry {entry_id!r}. list_my_changes shows valid ids.")
        if entry.get("outcome") != ledger.APPLIED:
            raise ToolError(
                f"entry {entry_id} is recorded as {entry.get('outcome')!r}, not "
                f"'applied'. Only a change that definitely landed can be reverted; "
                f"for a 'partial' or an unfinished one, run reconcile_ledger and "
                f"look at what actually happened first."
            )
        if entry_id in ledger.reverted_entry_ids():
            raise ToolError(f"entry {entry_id} has already been reverted.")

        tool = entry.get("tool")
        before, after = entry.get("before"), entry.get("after")
        if tool == "apply_negative_keywords_add":
            raise ToolError(
                "that entry created negative keywords, and deletion is not exposed "
                "by this server. Pause them with preview_negative_keyword_pause / "
                "apply_negative_keyword_pause instead."
            )
        if tool == "apply_keyword_bids_bulk":
            raise ToolError(
                "reverting a bulk bid change is not supported in one call: some "
                "items may have failed. Read list_my_changes for the per-item "
                "results and revert the ones you want individually with "
                "preview_keyword_bid."
            )
        if before in (None, "", "none"):
            raise ToolError(
                f"entry {entry_id} recorded no previous value, so there is nothing "
                f"to put back."
            )

        reverters = {
            "apply_keyword_bid": _revert_keyword_bid,
            "apply_keyword_status": _revert_keyword_status,
            "apply_ad_group_status": _revert_ad_group_status,
            "apply_ad_group_default_bid": _revert_ad_group_default_bid,
            "apply_campaign_status": _revert_campaign_status,
            "apply_campaign_daily_budget": _revert_campaign_budget,
            "apply_negative_keyword_pause": _revert_negative_status,
        }
        reverter = reverters.get(str(tool))
        if reverter is None:
            raise ToolError(f"no revert path for {tool!r}.")
        return reverter(entry, before, after)

    # --- revert implementations ---------------------------------------------

    def _begin_revert(entry: dict[str, Any], before: str, after: str, field: str) -> str:
        new_entry_id = ledger.new_entry_id()
        ledger.write_intent(
            new_entry_id,
            tool="revert_change",
            entity_type=str(entry.get("entity_type")),
            entity_id=entry.get("entity_id"),
            entity_name=entry.get("entity_name"),
            path=str(entry.get("path")),
            field=field,
            before=after,  # the revert's "before" is the original's "after"
            after=before,
            campaign_id=entry.get("campaign_id"),
            projected_daily_spend_delta="0",
            reverts_entry_id=str(entry.get("entry_id")),
        )
        return new_entry_id

    def _revert_result(
        new_entry_id: str, entry: dict[str, Any], before: str, after: str, observed: str | None
    ) -> ApplyResult:
        ledger.write_outcome(new_entry_id, outcome=ledger.APPLIED, observed_after=observed)
        state.counters.record(Decimal("0"))
        return ApplyResult(
            applied=True,
            entry_id=new_entry_id,
            tool="revert_change",
            entity_type=str(entry.get("entity_type")),
            entity_id=str(entry.get("entity_id")),
            entity_name=entry.get("entity_name"),
            path=str(entry.get("path")),
            field=str(entry.get("field")),
            before=after,
            after=before,
            observed_after=observed,
            projected_daily_spend_delta="0",
            session={k: str(v) for k, v in state.counters.snapshot().items()},
            note=f"Reverted entry {entry.get('entry_id')}.",
        )

    def _revert_keyword_bid(entry, before, after) -> ApplyResult:
        keyword_id = int(entry["entity_id"])
        keyword = client.get_keyword(keyword_id)
        current = money_amount(keyword.bid)
        confirm_unchanged(
            str(current) if current is not None else "none", str(after), f"keyword {keyword_id} bid"
        )
        new_entry_id = _begin_revert(entry, before, after, "bid")
        response = run_write(
            new_entry_id,
            "keywords_id_put",
            id=str(keyword_id),
            x_ap_context=context_header(),
            keyword_update=KeywordUpdate(
                bid=to_money(before, money_currency(keyword.bid))
            ),
        )
        updated = unwrap(response, "keyword update")
        return _revert_result(
            new_entry_id, entry, before, after, format_money(getattr(updated, "bid", None))
        )

    def _revert_keyword_status(entry, before, after) -> ApplyResult:
        keyword_id = int(entry["entity_id"])
        keyword = client.get_keyword(keyword_id)
        confirm_unchanged(
            enum_value(keyword.status) or "unknown", str(after), f"keyword {keyword_id} status"
        )
        new_entry_id = _begin_revert(entry, before, after, "status")
        response = run_write(
            new_entry_id,
            "keywords_id_put",
            id=str(keyword_id),
            x_ap_context=context_header(),
            keyword_update=KeywordUpdate(status=KeywordStatus(before)),
        )
        updated = unwrap(response, "keyword update")
        return _revert_result(
            new_entry_id, entry, before, after, enum_value(getattr(updated, "status", None))
        )

    def _revert_ad_group_status(entry, before, after) -> ApplyResult:
        ad_group_id = int(entry["entity_id"])
        ad_group = client.get_ad_group(ad_group_id)
        confirm_unchanged(
            enum_value(ad_group.status) or "unknown", str(after), f"ad group {ad_group_id} status"
        )
        new_entry_id = _begin_revert(entry, before, after, "status")
        response = run_write(
            new_entry_id,
            "adgroups_id_put",
            id=str(ad_group_id),
            x_ap_context=context_header(),
            ad_group_update=AdGroupUpdate(status=AdGroupStatus(before)),
        )
        updated = unwrap(response, "ad group update")
        return _revert_result(
            new_entry_id, entry, before, after, enum_value(getattr(updated, "status", None))
        )

    def _revert_ad_group_default_bid(entry, before, after) -> ApplyResult:
        ad_group_id = int(entry["entity_id"])
        ad_group = client.get_ad_group(ad_group_id)
        current_bid = _ad_group_bid(ad_group)
        current = money_amount(current_bid)
        confirm_unchanged(
            str(current) if current is not None else "none",
            str(after),
            f"ad group {ad_group_id} default bid",
        )
        new_entry_id = _begin_revert(entry, before, after, "default_bid")
        response = run_write(
            new_entry_id,
            "adgroups_id_put",
            id=str(ad_group_id),
            x_ap_context=context_header(),
            ad_group_update=AdGroupUpdate(
                bid_strategy=_bid_strategy_update(
                    before,
                    money_currency(current_bid),
                    _bid_strategy_type(ad_group),
                    _bid_strategy_goal(ad_group),
                )
            ),
        )
        updated = unwrap(response, "ad group update")
        return _revert_result(
            new_entry_id, entry, before, after, format_money(_ad_group_bid(updated))
        )

    def _revert_campaign_status(entry, before, after) -> ApplyResult:
        campaign_id = int(entry["entity_id"])
        campaign = client.get_campaign(campaign_id)
        confirm_unchanged(
            enum_value(campaign.status) or "unknown", str(after), f"campaign {campaign_id} status"
        )
        new_entry_id = _begin_revert(entry, before, after, "status")
        response = run_write(
            new_entry_id,
            "campaigns_id_put",
            id=str(campaign_id),
            x_ap_context=context_header(),
            campaign_update=CampaignUpdate(status=CampaignStatus(before)),
        )
        updated = unwrap(response, "campaign update")
        return _revert_result(
            new_entry_id, entry, before, after, enum_value(getattr(updated, "status", None))
        )

    def _revert_campaign_budget(entry, before, after) -> ApplyResult:
        campaign_id = int(entry["entity_id"])
        campaign = client.get_campaign(campaign_id)
        current = campaign.daily_budget.value if campaign.daily_budget else None
        current_amount = money_amount(current)
        confirm_unchanged(
            str(current_amount) if current_amount is not None else "none",
            str(after),
            f"campaign {campaign_id} daily budget",
        )
        new_entry_id = _begin_revert(entry, before, after, "daily_budget")
        response = run_write(
            new_entry_id,
            "campaigns_id_put",
            id=str(campaign_id),
            x_ap_context=context_header(),
            campaign_update=CampaignUpdate(
                daily_budget=DailyBudgetUpdate(value=to_money(before, money_currency(current)))
            ),
        )
        updated = unwrap(response, "campaign update")
        observed = updated.daily_budget.value if updated and updated.daily_budget else None
        return _revert_result(
            new_entry_id, entry, before, after, format_money(observed) if observed else None
        )

    def _revert_negative_status(entry, before, after) -> ApplyResult:
        negative_id = int(entry["entity_id"])
        negative = client.get_negative_keyword(negative_id)
        confirm_unchanged(
            enum_value(negative.status) or "unknown",
            str(after),
            f"negative keyword {negative_id} status",
        )
        new_entry_id = _begin_revert(entry, before, after, "status")
        response = run_write(
            new_entry_id,
            "negative_keywords_id_put",
            id=str(negative_id),
            x_ap_context=context_header(),
            negative_keyword_update=NegativeKeywordUpdate(status=NegativeKeywordStatus(before)),
        )
        updated = unwrap(response, "negative keyword update")
        return _revert_result(
            new_entry_id, entry, before, after, enum_value(getattr(updated, "status", None))
        )


# --- module-level helpers -----------------------------------------------------


def _bid_strategy_type(ad_group: Any) -> str | None:
    """MANUAL_CPT / MAX_CONVERSIONS / ... off an ad group, or None if unreadable."""
    strategy = getattr(ad_group, "bid_strategy", None)
    if strategy is None:
        return None
    return enum_value(getattr(strategy, "bid_strategy_type", None))


def _bid_strategy_goal(ad_group: Any) -> str | None:
    """TAP / INSTALL / ... off an ad group, or None if unreadable."""
    strategy = getattr(ad_group, "bid_strategy", None)
    if strategy is None:
        return None
    return enum_value(getattr(strategy, "bid_strategy_goal", None))


def _ad_group_bid(ad_group: Any) -> Any:
    """The `Money` at `bidStrategy.bid`, or None.

    There is no `defaultBid` field on an ad group or on AdGroupUpdate; the bid
    lives inside the nested bidStrategy object. Verified against the live API
    2026-10-09, and `tools_read._ad_group_row` reads it from the same place.
    """
    strategy = getattr(ad_group, "bid_strategy", None)
    if strategy is None:
        return None
    return getattr(strategy, "bid", None)


def _bid_strategy_update(
    amount: str,
    currency: str | None,
    strategy_type: str | None,
    strategy_goal: str | None,
) -> BidStrategyUpdate:
    """The new bid, with the ad group's existing strategy echoed back unchanged.

    `bidStrategy` is a NESTED object, and Apple's PUT merges at the top level.
    Sending `{"bidStrategy": {"bid": ...}}` alone therefore risks replacing the
    whole object and dropping bidStrategyType with it -- which would move the ad
    group onto a different bidding model, a far larger change than the one in the
    preview. Echoing both fields back makes the write a no-op for everything
    except the number a human approved.
    """
    kwargs: dict[str, Any] = {"bid": to_money(amount, currency)}
    if strategy_type:
        kwargs["bid_strategy_type"] = BidStrategyType(strategy_type)
    if strategy_goal:
        kwargs["bid_strategy_goal"] = BidStrategyGoal(strategy_goal)
    return BidStrategyUpdate(**kwargs)


def _campaign_currency(campaign: Any) -> str | None:
    """The campaign's budget currency, for an ad group that has no bid yet."""
    budget = getattr(campaign, "daily_budget", None) if campaign is not None else None
    return money_currency(getattr(budget, "value", None)) if budget is not None else None


def _default_bid_blast_radius(ad_group_id: int) -> list[str]:
    """How many keywords this default bid actually governs.

    A keyword carrying a bid of its own ignores the ad group default, so "this
    reprices 40 keywords" would be false where 38 are explicitly bid. Degrades to
    a warning that says so rather than failing the preview: the rest of the
    preview is still worth reading if this one query fails.
    """
    from apple_ads_mcp.client import eq_filter, query

    try:
        response = call(
            "keywords_query_post",
            x_ap_context=context_header(),
            query_request=query(page_size=1000, filters=[eq_filter("adGroupId", ad_group_id)]),
        )
        rows = [k for k in (unwrap(response, "keywords") or []) if not k.deleted]
    except ToolError as exc:
        return [
            "Could not list this ad group's keywords, so this preview cannot say how "
            f"many of them the default bid governs: {str(exc)[:160]}"
        ]
    if not rows:
        return [
            "This ad group has NO keywords, so the default bid is the only bid it "
            "has -- every impression it wins is priced off this one number. That is "
            "normal for a Search Tab or Search Match ad group."
        ]
    own = sum(1 for k in rows if money_amount(getattr(k, "bid", None)) is not None)
    return [
        f"This ad group has {len(rows)} keyword(s): {len(rows) - own} fall back to "
        f"the default bid and are repriced by this change; {own} carry their own bid "
        f"and are NOT affected."
    ]


def _status(raw: str, allowed: set[str], field: str = "status") -> str:
    value = str(raw).strip().upper()
    if value not in allowed:
        raise ToolError(f"{field}={raw!r} must be one of {', '.join(sorted(allowed))}")
    return value


def _existing_negatives(campaign_id: int | None, ad_group_id: int | None) -> set[tuple[str, str]]:
    """(lowercased text, match type) already present, for the duplicate warning."""
    from apple_ads_mcp.client import eq_filter, query

    filters = []
    if ad_group_id is not None:
        filters.append(eq_filter("adGroupId", ad_group_id))
    elif campaign_id is not None:
        filters.append(eq_filter("campaignId", campaign_id))
    try:
        rows = unwrap(
            call(
                "negative_keywords_query_post",
                x_ap_context=context_header(),
                query_request=query(page_size=1000, filters=filters or None),
            ),
            "negative keywords",
        ) or []
    except ToolError:
        return set()
    return {
        ((row.text or "").lower(), enum_value(row.match_type) or "")
        for row in rows
        if not row.deleted
    }
