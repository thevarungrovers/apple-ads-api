"""Keyword bids and status, and negative keywords."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from apple_ads_platform import (
    BulkKeywordCreate,
    BulkKeywordUpdate,
    BulkNegativeKeywordUpdate,
    KeywordCreateBulkRequest,
    KeywordCreateBulkRequestItem,
    KeywordMatchType,
    NegativeKeywordUpdateBulkRequest,
    NegativeKeywordUpdateBulkRequestItem,
    BulkNegativeKeywordCreate,
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
from pydantic import Field

from apple_ads_mcp import client
from apple_ads_mcp.changes.base import (
    APPLY,
    CREATED,
    NOTHING,
    ChangeKind,
    Outcome,
    Proposal,
    item as make_item,
)
from apple_ads_mcp.changes.common import (
    SETTABLE_STATUSES,
    ad_group_bid,
    campaign_currency,
    StatusKind,
    bid_strategy_type,
    bound_check,
    child_preview,
    existing_negatives,
    metrics_block,
    metrics_warning,
    parse_choice,
    refuse_if_moved,
    serving_warning,
)
from apple_ads_mcp.client import (
    context_header,
    eq_filter,
    entity_path,
    enum_value,
    format_money,
    landed,
    money_amount,
    money_currency,
    parse_decimal,
    to_money,
    unwrap,
)
from apple_ads_mcp.guardrails import (
    check_bid_settable,
    check_campaign_in_scope,
    check_ceiling,
    check_currency,
    check_known_status,
    check_pct_increase,
    check_projected_delta,
)
from apple_ads_mcp.previews import any_failed, pct_change, project_bid_change, project_pause, render_checks

BID_NOTE = (
    "projected_daily_spend_delta is an UPPER BOUND: it assumes tap volume is "
    "unchanged, which a bid change is precisely intended to alter. Use it to catch "
    "a 100x typo, not as a forecast."
)


def _bulk_outcomes(response: Any, items: list[dict[str, Any]], what: str, read) -> list[Outcome]:
    """Per-item outcomes of a bulk endpoint, tied back by correlationId.

    HTTP 200 from a bulk endpoint does NOT mean every item applied: Apple
    returns a per-item success/error alongside the correlationId we set. And an
    item's `success` is Apple's acknowledgement, not evidence the value moved --
    under an automated bid strategy it reports success and echoes the OLD bid.
    """
    results = unwrap(response, what) or []
    by_correlation = {r.correlation_id: r for r in results}
    outcomes = []
    for index, item in enumerate(items):
        result = by_correlation.get(index)
        if result is None:
            outcomes.append(Outcome(False, None, error="Apple returned no result for it"))
        elif not result.success:
            outcomes.append(Outcome(False, None, error=str(result.error)))
        else:
            outcomes.append(read(result.result, item))
    return outcomes


class KeywordBid(ChangeKind):
    name = "keyword_bid"
    preview_title = "Preview a keyword bid change (no write)"
    apply_title = "Apply a keyword bid change (CHANGES WHAT THIS KEYWORD COSTS)"
    apply_annotations = APPLY
    ledger_keys = (("keyword", "bid"),)

    def propose(
        self,
        ctx,
        keyword_id: Annotated[int, Field(description="Apple keyword id")],
        new_bid: Annotated[
            str,
            Field(description='New bid as a decimal string in major units, e.g. "1.20"'),
        ],
    ) -> Proposal:
        """Show what changing one keyword's bid would do. Writes nothing.

        Returns a preview_token for apply_keyword_bid. Check
        campaign_serving_status and projected_daily_spend_delta before proposing
        it to a human.
        """
        built = keyword_bid_item(ctx, keyword_id, new_bid, "new_bid")
        return Proposal(
            items=[built["item"]],
            checks=built["checks"],
            projected=built["projected"],
            warnings=built["warnings"],
            note=BID_NOTE,
            entity_type="keyword",
            entity_id=str(keyword_id),
            entity_name=built["keyword"].text,
            path=built["item"]["path"],
            field_name="bid",
            before=built["before_text"],
            after=built["after_text"],
            before_num=built["before_num"],
            after_num=built["after_num"],
            campaign=built["campaign"],
            entity_serving=enum_value(built["keyword"].display_status),
            metrics=built["metrics"],
        )

    def current(self, ctx, item):
        keyword = client.get_keyword(item["entity_id"])
        current = money_amount(keyword.bid)
        if current is None and "inherited_default" in item:
            # An inheriting keyword's preview measured the change -- projection,
            # % increase -- against the ad group default, so that is stale too.
            default = money_amount(ad_group_bid(client.get_ad_group(int(keyword.ad_group_id))))
            refuse_if_moved(
                str(default) if default is not None else "none",
                str(item["inherited_default"]) if item["inherited_default"] is not None else "none",
                f"ad group {keyword.ad_group_id} default bid (which keyword "
                f"{item['entity_id']} pays)",
            )
        return str(current) if current is not None else "none"

    def request(self, ctx, items):
        item = items[0]
        return "keywords_id_put", {
            "id": str(item["entity_id"]),
            "x_ap_context": context_header(),
            "keyword_update": KeywordUpdate(bid=to_money(item["after"], item.get("currency"))),
        }

    def observe(self, ctx, response, items):
        updated = unwrap(response, "keyword update")
        observed = format_money(getattr(updated, "bid", None)) if updated else None
        return [Outcome(landed(observed, items[0]["after"]), observed)]

    def inverse(self, ctx, entry):
        keyword = client.get_keyword(int(entry["entity_id"]))
        return self, make_item(
            entity_type="keyword",
            entity_id=entry["entity_id"],
            entity_name=entry.get("entity_name"),
            path=str(entry.get("path")),
            field="bid",
            before=str(entry["after"]),
            after=str(entry["before"]),
            campaign_id=entry.get("campaign_id"),
            currency=money_currency(keyword.bid),
        )


def keyword_bid_item(ctx, keyword_id: int, raw_bid: str, label: str) -> dict[str, Any]:
    """Everything a keyword bid preview needs, for one keyword."""
    after_num = parse_decimal(raw_bid, label)
    keyword = client.get_keyword(keyword_id)
    ad_group = ctx.ad_group(keyword.ad_group_id)
    campaign = ctx.campaign(keyword.campaign_id)
    own_bid = money_amount(keyword.bid)
    # A keyword with no bid of its own pays the ad group default. Using its own
    # (absent) bid as `before` made the projection 0, the % check pass with "no
    # previous value", and the currency check FAIL for want of a currency -- so
    # the effective bid is what the change is measured against.
    inherited = own_bid is None
    effective = keyword.bid if not inherited else ad_group_bid(ad_group)
    before_num = money_amount(effective)
    currency = money_currency(effective) or campaign_currency(campaign) or None
    spend, taps, impressions, installs, metrics_error = ctx.metrics_7d(
        "apps_keyword_reports", "keywordId", keyword_id, keyword.campaign_id
    )
    projected = project_bid_change(taps, before_num, after_num)
    checks = [
        check_campaign_in_scope(ctx.limits, keyword.campaign_id),
        check_currency(ctx.limits, currency),
        check_ceiling("max_bid", after_num, ctx.limits.max_bid, currency or ""),
        check_pct_increase(
            "max_bid_increase_pct", before_num, after_num, ctx.limits.max_bid_increase_pct
        ),
        check_known_status("keyword_status_known", enum_value(keyword.status)),
        check_bid_settable(bid_strategy_type(ad_group)),
    ]
    warnings = serving_warning(enum_value(keyword.display_status), "keyword") + metrics_warning(
        metrics_error
    )
    if enum_value(keyword.status) != "ENABLED":
        warnings.append(
            "The keyword itself is PAUSED, so this bid will not spend until it is enabled."
        )
    if inherited:
        warnings.append(
            f"This keyword has no bid of its own: it pays the ad group default "
            f"({format_money(effective)}), which is what this change is measured "
            f"against. Setting a bid detaches it from the default for good, and "
            f"revert_change cannot put it back to inheriting -- it will refuse this entry."
        )
    path = entity_path(campaign, ad_group, keyword.text)
    return {
        "keyword": keyword,
        "campaign": campaign,
        "checks": checks,
        "warnings": warnings,
        "projected": projected,
        "before_num": before_num,
        "after_num": after_num,
        "before_text": format_money(effective) + (" (ad group default)" if inherited else ""),
        "after_text": f"{after_num} {currency or ''}".strip(),
        "metrics": metrics_block(spend, taps, impressions, installs),
        "item": make_item(
            entity_type="keyword",
            entity_id=keyword_id,
            entity_name=keyword.text,
            path=path,
            field="bid",
            # The keyword's OWN bid: that is what staleness re-reads and what a
            # revert would write back. None when it inherits.
            before=str(own_bid) if own_bid is not None else None,
            after=str(after_num),
            campaign_id=keyword.campaign_id,
            projected=projected,
            currency=currency,
            **(
                {"inherited_default": str(before_num) if before_num is not None else None}
                if inherited
                else {}
            ),
        ),
    }


class KeywordBidsBulk(ChangeKind):
    name = "keyword_bids_bulk"
    preview_title = "Preview several keyword bid changes at once (no write)"
    apply_title = "Apply several keyword bid changes (CHANGES WHAT THESE KEYWORDS COST)"
    apply_annotations = APPLY
    bulk = True
    # Its rows are ("keyword", "bid") -- KeywordBid owns that key, and reverts
    # one item with a single-keyword PUT.
    ledger_keys = ()

    def propose(
        self,
        ctx,
        keyword_ids: Annotated[
            list[int], Field(description="Keyword ids to change, in the same order as new_bids")
        ],
        new_bids: Annotated[
            list[str],
            Field(description='New bids as decimal strings, e.g. ["1.20", "0.85"]'),
        ],
    ) -> Proposal:
        """Show what a batch of bid changes would do. Writes nothing.

        Every item is previewed individually -- each against the per-change
        limit -- and only the increases are summed (a cut never offsets a
        raise), so one out-of-bounds bid blocks the whole batch rather than
        slipping through inside a total that looks reasonable.
        """
        if len(keyword_ids) != len(new_bids):
            raise ToolError(
                f"keyword_ids has {len(keyword_ids)} entries and new_bids has "
                f"{len(new_bids)}; they are positional and must be the same length."
            )
        if not keyword_ids:
            raise ToolError("keyword_ids is empty; there is nothing to preview.")

        items, children, all_checks = [], [], []
        total = Decimal("0")
        for keyword_id, raw_bid in zip(keyword_ids, new_bids):
            built = keyword_bid_item(ctx, keyword_id, raw_bid, f"new_bids[{keyword_id}]")
            # Each item answers to the per-change limit on its own, and only
            # INCREASES count towards the total: a cut on one keyword does not
            # pay for a raise on another, any more than it does in the session
            # and daily caps (which sum each row's positive delta).
            per_item = check_projected_delta(ctx.limits, built["projected"])
            per_item.detail = f"keyword {keyword_id}: {per_item.detail}"
            built["checks"].append(per_item)
            total += max(built["projected"], Decimal("0"))
            all_checks.extend(built["checks"])
            items.append(built["item"])
            keyword = built["keyword"]
            children.append(
                child_preview(
                    tool="apply_keyword_bids_bulk",
                    entity_type="keyword",
                    entity_id=keyword_id,
                    entity_name=keyword.text,
                    path=built["item"]["path"],
                    field="bid",
                    before=built["before_text"],
                    after=built["after_text"],
                    campaign=built["campaign"],
                    entity_serving=enum_value(keyword.display_status),
                    projected=built["projected"],
                    checks=render_checks(built["checks"]),
                    blocked=any_failed(built["checks"]),
                    metrics=built["metrics"],
                    pct=pct_change(built["before_num"], built["after_num"]),
                )
            )
        return Proposal(
            items=items,
            checks=all_checks,
            projected=total,
            children=children,
            warnings=[
                "A bulk write can come back HTTP 200 with some items rejected. "
                "apply_keyword_bids_bulk reports per-item results rather than a "
                "single success."
            ],
            note="One out-of-bounds item blocks the whole batch.",
        )

    def current(self, ctx, item):
        return KeywordBid.current(self, ctx, item)

    def request(self, ctx, items):
        # correlationId is always set, so a per-item result can be tied back to
        # the request item it came from. Without it a partial failure is just a
        # list of errors with no way to say WHICH keyword did not change.
        return "keywords_bulk_update_post", {
            "x_ap_context": context_header(),
            "keyword_update_bulk_request": KeywordUpdateBulkRequest(
                allow_partial_success=True,
                items=[
                    KeywordUpdateBulkRequestItem(
                        correlation_id=index,
                        data=BulkKeywordUpdate(
                            id=int(item["entity_id"]),
                            bid=to_money(item["after"], item.get("currency")),
                        ),
                    )
                    for index, item in enumerate(items)
                ],
            ),
        }

    def observe(self, ctx, response, items):
        def read(result, item):
            observed = format_money(getattr(result, "bid", None))
            return Outcome(landed(observed, item["after"]), observed)

        return _bulk_outcomes(response, items, "bulk keyword update", read)


class KeywordStatusKind(StatusKind):
    name = "keyword_status"
    preview_title = "Preview pausing or enabling a keyword (no write)"
    apply_title = "Apply a keyword pause or enable (CHANGES LIVE DELIVERY)"
    apply_annotations = APPLY
    entity_type = "keyword"
    label = "keyword"
    get_method = "keywords_id_get"
    put_method = "keywords_id_put"
    update_arg = "keyword_update"
    ledger_keys = (("keyword", "status"),)

    def update_body(self, status):
        return KeywordUpdate(status=KeywordStatus(status))

    def propose(
        self,
        ctx,
        keyword_id: Annotated[int, Field(description="Apple keyword id")],
        status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> Proposal:
        """Show what pausing or enabling one keyword would do. Writes nothing."""
        target = parse_choice(status, SETTABLE_STATUSES)
        keyword = client.get_keyword(keyword_id)
        ad_group = ctx.ad_group(keyword.ad_group_id)
        campaign = ctx.campaign(keyword.campaign_id)
        before = enum_value(keyword.status)
        if before == target:
            raise ToolError(f"keyword {keyword_id} is already {target}; nothing to change.")
        spend, taps, impressions, installs, metrics_error = ctx.metrics_7d(
            "apps_keyword_reports", "keywordId", keyword_id, keyword.campaign_id
        )
        bound_checks = []
        if target == "PAUSED":
            projected = project_pause(spend)
        else:
            projected, bound_checks = bound_check(*self.enable_bound(ctx, keyword.campaign_id))
        entity_serving = enum_value(keyword.display_status)
        warnings = serving_warning(entity_serving, "keyword") + metrics_warning(metrics_error)
        if target == "ENABLED":
            warnings.append(
                "Enabling a keyword RESUMES spend on it. The projection is the "
                "campaign's unused daily budget -- the most it could add -- not a "
                "forecast."
            )
        path = entity_path(campaign, ad_group, keyword.text)
        return Proposal(
            items=[
                make_item(
                    entity_type="keyword",
                    entity_id=keyword_id,
                    entity_name=keyword.text,
                    path=path,
                    field="status",
                    before=before,
                    after=target,
                    campaign_id=keyword.campaign_id,
                    projected=projected,
                )
            ],
            checks=[
                check_campaign_in_scope(ctx.limits, keyword.campaign_id),
                check_known_status("keyword_status_known", before),
                *bound_checks,
            ],
            projected=projected,
            is_enable=target == "ENABLED",
            warnings=warnings,
            note="Reversible: apply the opposite status to undo, or use revert_change.",
            entity_type="keyword",
            entity_id=str(keyword_id),
            entity_name=keyword.text,
            path=path,
            field_name="status",
            before=before or "unknown",
            after=target,
            campaign=campaign,
            entity_serving=entity_serving,
            metrics=metrics_block(spend, taps, impressions, installs),
        )


class NegativeKeywordStatusKind(StatusKind):
    name = "negative_keyword_pause"
    preview_title = "Preview pausing or enabling a negative keyword (no write)"
    apply_title = "Apply a negative keyword pause or enable (CHANGES WHAT TRAFFIC IS EXCLUDED)"
    apply_annotations = APPLY
    entity_type = "negative_keyword"
    label = "negative keyword"
    get_method = "negative_keywords_id_get"
    put_method = "negative_keywords_id_put"
    update_arg = "negative_keyword_update"
    ledger_keys = (("negative_keyword", "status"),)

    def update_body(self, status):
        return NegativeKeywordUpdate(status=NegativeKeywordStatus(status))

    def revert_projection(self, ctx, entry, item):
        # Inverted: it is PAUSING a negative that lets traffic back in.
        if item["after"] != "PAUSED":
            return Decimal("0"), False
        bound, reason = self.enable_bound(ctx, item.get("campaign_id"))
        if bound is None:
            raise ValueError(f"cannot revert: {reason}.")
        return bound, True

    def propose(
        self,
        ctx,
        negative_keyword_id: Annotated[int, Field(description="Apple negative keyword id")],
        status: Annotated[
            str, Field(description="PAUSED to stop excluding, ENABLED to resume")
        ] = "PAUSED",
    ) -> Proposal:
        """Show what pausing or enabling a negative keyword would do. Writes nothing.

        Pausing a negative keyword lets its traffic back in, so it can INCREASE
        spend. That is the reverse of what "pause" usually implies here.
        """
        target = parse_choice(status, SETTABLE_STATUSES)
        negative = client.get_negative_keyword(negative_keyword_id)
        before = enum_value(negative.status)
        if before == target:
            raise ToolError(
                f"negative keyword {negative_keyword_id} is already {target}; nothing to change."
            )
        campaign = ctx.campaign(negative.campaign_id)
        ad_group = ctx.ad_group(negative.ad_group_id)
        path = entity_path(campaign, ad_group, negative.text, keyword_label="NegativeKeyword")
        entity_serving = enum_value(getattr(campaign, "display_status", None))
        warnings = serving_warning(entity_serving, "campaign")
        bound_checks = []
        projected = Decimal("0")
        if target == "PAUSED":
            warnings.append(
                "Pausing a NEGATIVE keyword stops excluding this term, so traffic "
                "and spend can go UP, not down. The projection is the campaign's "
                "unused daily budget -- the most the re-admitted traffic could add."
            )
            projected, bound_checks = bound_check(*self.enable_bound(ctx, negative.campaign_id))
        return Proposal(
            items=[
                make_item(
                    entity_type="negative_keyword",
                    entity_id=negative_keyword_id,
                    entity_name=negative.text,
                    path=path,
                    field="status",
                    before=before,
                    after=target,
                    campaign_id=negative.campaign_id,
                    projected=projected,
                )
            ],
            checks=[
                check_campaign_in_scope(ctx.limits, negative.campaign_id),
                check_known_status("negative_keyword_status_known", before),
                *bound_checks,
            ],
            projected=projected,
            is_enable=target == "PAUSED",
            warnings=warnings,
            note=(
                "Pausing is the supported way to undo a negative keyword. Deletion "
                "is deliberately not exposed -- it would be the only irreversible "
                "operation here."
            ),
            entity_type="negative_keyword",
            entity_id=str(negative_keyword_id),
            entity_name=negative.text,
            path=path,
            field_name="status",
            before=before or "unknown",
            after=target,
            campaign=campaign,
            entity_serving=entity_serving,
        )


class NegativeKeywordsAdd(ChangeKind):
    name = "negative_keywords_add"
    preview_title = "Preview adding negative keywords (no write)"
    apply_title = "Apply negative keywords (EXCLUDES TRAFFIC; not idempotent)"
    apply_annotations = APPLY
    bulk = True
    ledger_keys = (("negative_keyword", CREATED),)
    success_note = (
        "To undo one, revert_change its entry id: that PAUSES the negative -- "
        "deletion is not exposed."
    )

    def propose(
        self,
        ctx,
        texts: Annotated[list[str], Field(description="Search terms to exclude")],
        match_type: Annotated[str, Field(description="EXACT or BROAD")] = "EXACT",
        campaign_id: Annotated[
            int | None, Field(description="Add at campaign level; omit if using ad_group_id")
        ] = None,
        ad_group_id: Annotated[
            int | None, Field(description="Add at ad group level; omit if using campaign_id")
        ] = None,
    ) -> Proposal:
        """Show what adding these negative keywords would do. Writes nothing.

        A negative keyword stops matching traffic, so its effect on spend is
        negative or zero -- but it is also NOT idempotent: running it twice
        creates two of each. They are created ENABLED, because a paused negative
        excludes nothing. Undo by pausing (revert_change does that); deletion is
        not exposed.
        """
        target_match = parse_choice(match_type, {"EXACT", "BROAD"}, "match_type")
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
            ad_group = ctx.ad_group(ad_group_id)
            resolved_campaign_id = ad_group.campaign_id
            campaign = ctx.campaign(resolved_campaign_id)
            scope = entity_path(campaign, ad_group)
        else:
            ad_group = None
            resolved_campaign_id = campaign_id
            campaign = ctx.campaign(campaign_id)
            scope = entity_path(campaign)

        existing, unchecked = existing_negatives(resolved_campaign_id, ad_group_id)
        duplicates = [t for t in cleaned if (t.lower(), target_match) in existing]
        serving = (
            enum_value(getattr(ad_group, "display_status", None))
            if ad_group is not None
            else enum_value(getattr(campaign, "display_status", None))
        )
        items, children = [], []
        for text in cleaned:
            path = f"{scope} > NegativeKeyword {text!r}"
            items.append(
                make_item(
                    entity_type="negative_keyword",
                    entity_id=None,
                    entity_name=text,
                    path=path,
                    field=CREATED,
                    before=NOTHING,
                    after="ENABLED",
                    campaign_id=resolved_campaign_id,
                    text=text,
                    match_type=target_match,
                    ad_group_id=ad_group_id,
                )
            )
            children.append(
                child_preview(
                    tool="apply_negative_keywords_add",
                    entity_type="negative_keyword",
                    entity_id="(new)",
                    entity_name=text,
                    path=path,
                    field="exists",
                    before="absent",
                    after=f"{target_match} negative",
                    campaign=campaign,
                    entity_serving=serving,
                    projected=Decimal("0"),
                    warnings=["already present -- adding it again creates a duplicate"]
                    if (text.lower(), target_match) in existing
                    else [],
                )
            )
        warnings = serving_warning(serving, "ad group" if ad_group is not None else "campaign")
        warnings.append("NOT idempotent: applying this twice creates duplicate negatives.")
        if unchecked:
            warnings.append(unchecked)
        if duplicates:
            warnings.append(
                f"{len(duplicates)} of these already exist as {target_match} negatives: "
                + ", ".join(repr(d) for d in duplicates[:10])
            )
        return Proposal(
            items=items,
            checks=[check_campaign_in_scope(ctx.limits, resolved_campaign_id)],
            projected=Decimal("0"),
            children=children,
            warnings=warnings,
            note=(
                "Adding a negative can only reduce spend, so the projection is 0 "
                "rather than an estimate of the saving. To undo, PAUSE the "
                "negative -- deletion is not exposed."
            ),
        )

    def current(self, ctx, item):
        return None  # nothing exists yet to compare against

    def request(self, ctx, items):
        return "negative_keywords_bulk_create_post", {
            "x_ap_context": context_header(),
            "negative_keyword_create_bulk_request": NegativeKeywordCreateBulkRequest(
                allow_partial_success=True,
                items=[
                    NegativeKeywordCreateBulkRequestItem(
                        correlation_id=index,
                        data=BulkNegativeKeywordCreate(
                            campaign_id=item["campaign_id"],
                            ad_group_id=item.get("ad_group_id"),
                            text=item["text"],
                            match_type=item["match_type"],
                            status=NegativeKeywordStatus("ENABLED"),
                        ),
                    )
                    for index, item in enumerate(items)
                ],
            ),
        }

    def observe(self, ctx, response, items):
        def read(result, item):
            new_id = getattr(result, "id", None)
            observed = enum_value(getattr(result, "status", None)) or (
                "ENABLED" if new_id is not None else None
            )
            return Outcome(
                new_id is not None and landed(observed, item["after"]),
                observed,
                created_id=str(new_id) if new_id is not None else None,
            )

        return _bulk_outcomes(response, items, "bulk negative keyword create", read)

    def inverse(self, ctx, entry):
        return NegativeKeywordStatusKind().undo_creation(entry)


# --- 3a: creating keywords, changing a match type, negatives in bulk ------------------


class AddKeywords(ChangeKind):
    name = "add_keywords"
    preview_title = "Preview adding keywords to an ad group, created PAUSED (no write)"
    apply_title = "Apply new keywords (created PAUSED; not idempotent)"
    apply_annotations = APPLY
    bulk = True
    ledger_keys = (("keyword", CREATED),)
    success_note = (
        "They were created PAUSED and spend nothing until enabled with "
        "preview_keyword_status. revert_change on an entry pauses that keyword; "
        "deletion is not exposed."
    )

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Ad group to add them to")],
        texts: Annotated[list[str], Field(description="Keyword texts to add")],
        match_type: Annotated[str, Field(description="EXACT or BROAD")] = "EXACT",
        bid: Annotated[
            str | None,
            Field(description='Bid for each, a decimal string like "1.20"; omit to use '
                  "the ad group default"),
        ] = None,
    ) -> Proposal:
        """Show what adding these keywords would do. Writes nothing.

        Every keyword is created PAUSED, so approving this spends nothing:
        enabling one is a separate preview_keyword_status, projected against the
        campaign's budget. NOT idempotent -- applying twice adds duplicates.
        """
        target_match = parse_choice(match_type, {"EXACT", "BROAD"}, "match_type")
        cleaned = list(dict.fromkeys(t.strip() for t in texts if t and t.strip()))
        if not cleaned:
            raise ToolError("texts is empty; there is nothing to add.")
        ad_group = ctx.ad_group(ad_group_id)
        campaign = ctx.campaign(ad_group.campaign_id)
        currency = money_currency(ad_group_bid(ad_group)) or campaign_currency(campaign)
        checks = [check_campaign_in_scope(ctx.limits, ad_group.campaign_id)]
        bid_num = None
        if bid is not None:
            bid_num = parse_decimal(bid, "bid")
            checks += [
                check_currency(ctx.limits, currency),
                check_ceiling("max_bid", bid_num, ctx.limits.max_bid, currency or ""),
                check_bid_settable(bid_strategy_type(ad_group)),
            ]
        try:
            existing_rows, _cut = client.query_all(
                "keywords_query_post", [eq_filter("adGroupId", ad_group_id)], cap=5000
            )
            existing = {
                ((k.text or "").lower(), enum_value(k.match_type)) for k in existing_rows if not k.deleted
            }
            unchecked = None
        except ToolError as exc:
            existing, unchecked = set(), f"duplicates could not be checked: {str(exc)[:160]}"
        scope = entity_path(campaign, ad_group)
        items, children = [], []
        for text in cleaned:
            path = f"{scope} > Keyword {text!r}"
            items.append(make_item(
                entity_type="keyword", entity_id=None, entity_name=text, path=path,
                field=CREATED, before=NOTHING, after="PAUSED", campaign_id=ad_group.campaign_id,
                ad_group_id=ad_group_id, text=text, match_type=target_match,
                bid=str(bid_num) if bid_num is not None else None, currency=currency,
            ))
            children.append(child_preview(
                tool="apply_add_keywords", entity_type="keyword", entity_id="(new)",
                entity_name=text, path=path, field="exists", before="absent",
                after=f"{target_match} keyword, PAUSED"
                + (f", bid {bid_num} {currency or ''}".rstrip() if bid_num is not None else ", default bid"),
                campaign=campaign, entity_serving="(created PAUSED)", projected=Decimal("0"),
                warnings=["already present with this match type -- adding it again creates a duplicate"]
                if (text.lower(), target_match) in existing else [],
            ))
        duplicates = [t for t in cleaned if (t.lower(), target_match) in existing]
        warnings = ["NOT idempotent: applying this twice adds every keyword twice."]
        if unchecked:
            warnings.append(unchecked)
        if duplicates:
            warnings.append(
                f"{len(duplicates)} of these already exist as {target_match} keywords: "
                + ", ".join(repr(d) for d in duplicates[:10])
            )
        return Proposal(
            items=items, checks=checks, projected=Decimal("0"), children=children,
            warnings=warnings,
            note=(
                "Created PAUSED, so the projection is 0: nothing serves until a keyword "
                "is enabled, and that change projects the campaign's unused budget."
            ),
        )

    def current(self, ctx, item):
        return None

    def request(self, ctx, items):
        return "keywords_bulk_create_post", {
            "x_ap_context": context_header(),
            "keyword_create_bulk_request": KeywordCreateBulkRequest(
                allow_partial_success=True,
                items=[
                    KeywordCreateBulkRequestItem(
                        correlation_id=index,
                        data=BulkKeywordCreate(
                            ad_group_id=int(item["ad_group_id"]),
                            text=item["text"],
                            match_type=KeywordMatchType(item["match_type"]),
                            bid=to_money(item["bid"], item.get("currency")) if item.get("bid") else None,
                            status=KeywordStatus("PAUSED"),
                        ),
                    )
                    for index, item in enumerate(items)
                ],
            ),
        }

    def observe(self, ctx, response, items):
        def read(result, item):
            new_id = getattr(result, "id", None)
            observed = enum_value(getattr(result, "status", None))
            return Outcome(
                new_id is not None and landed(observed, item["after"]),
                observed,
                created_id=str(new_id) if new_id is not None else None,
            )

        return _bulk_outcomes(response, items, "bulk keyword create", read)

    def inverse(self, ctx, entry):
        return KeywordStatusKind().undo_creation(entry)


class KeywordMatchTypeKind(ChangeKind):
    name = "keyword_match_type"
    preview_title = "Preview changing a keyword's match type: add a PAUSED copy, pause the old (no write)"
    apply_title = "Apply a match type change (adds a PAUSED keyword and PAUSES the old one)"
    apply_annotations = APPLY
    bulk = True
    ledger_keys = ()  # its rows are (keyword, created) and (keyword, status)
    success_note = (
        "Both keywords are now PAUSED. Enable the new one with "
        "preview_keyword_status when you are ready -- that is a separate change, "
        "projected against the campaign's budget. revert_change on the batch "
        "re-enables the old keyword."
    )

    def propose(
        self,
        ctx,
        keyword_id: Annotated[int, Field(description="The keyword whose match type to change")],
        new_match_type: Annotated[str, Field(description="EXACT or BROAD")],
    ) -> Proposal:
        """Show what changing a keyword's match type would do. Writes nothing.

        Apple does not let a keyword's match type be edited. The replacement, in
        one preview: add a new keyword with the same text, bid and the new match
        type, created PAUSED, and pause the old one. Both end PAUSED -- enable
        the new one separately when you are ready. revert_change on the batch
        re-enables the old keyword.
        """
        target = parse_choice(new_match_type, {"EXACT", "BROAD"}, "new_match_type")
        keyword = client.get_keyword(keyword_id)
        current_match = enum_value(keyword.match_type)
        if current_match == target:
            raise ToolError(f"keyword {keyword_id} is already {target}; nothing to change.")
        before_status = enum_value(keyword.status)
        ad_group = ctx.ad_group(keyword.ad_group_id)
        campaign = ctx.campaign(keyword.campaign_id)
        spend, taps, impressions, installs, metrics_error = ctx.metrics_7d(
            "apps_keyword_reports", "keywordId", keyword_id, keyword.campaign_id
        )
        pause_delta = project_pause(spend) if before_status == "ENABLED" else Decimal("0")
        bid = money_amount(keyword.bid)
        currency = money_currency(keyword.bid) or campaign_currency(campaign)
        scope = entity_path(campaign, ad_group)
        new_path = f"{scope} > Keyword {keyword.text!r} ({target})"
        old_path = entity_path(campaign, ad_group, keyword.text)
        items = [
            make_item(
                entity_type="keyword", entity_id=None, entity_name=keyword.text, path=new_path,
                field=CREATED, before=NOTHING, after="PAUSED", campaign_id=keyword.campaign_id,
                ad_group_id=keyword.ad_group_id, text=keyword.text, match_type=target,
                bid=str(bid) if bid is not None else None, currency=currency,
            ),
            make_item(
                entity_type="keyword", entity_id=keyword_id, entity_name=keyword.text,
                path=old_path, field="status", before=before_status, after="PAUSED",
                campaign_id=keyword.campaign_id, projected=pause_delta,
            ),
        ]
        children = [
            child_preview(
                tool="apply_keyword_match_type", entity_type="keyword", entity_id="(new)",
                entity_name=keyword.text, path=new_path, field="exists", before="absent",
                after=f"{target} keyword, PAUSED, bid {format_money(keyword.bid) if bid is not None else 'default'}",
                campaign=campaign, entity_serving="(created PAUSED)", projected=Decimal("0"),
            ),
            child_preview(
                tool="apply_keyword_match_type", entity_type="keyword", entity_id=keyword_id,
                entity_name=keyword.text, path=old_path, field="status",
                before=f"{before_status} ({current_match})", after="PAUSED",
                campaign=campaign, entity_serving=enum_value(keyword.display_status),
                projected=pause_delta, metrics=metrics_block(spend, taps, impressions, installs),
            ),
        ]
        warnings = serving_warning(enum_value(keyword.display_status), "keyword") + metrics_warning(metrics_error)
        warnings.append(
            "After this, NEITHER keyword serves until you enable the new one: the old "
            "is paused and the new is created paused."
        )
        return Proposal(
            items=items,
            checks=[
                check_campaign_in_scope(ctx.limits, keyword.campaign_id),
                check_known_status("keyword_status_known", before_status),
            ],
            projected=pause_delta, children=children, warnings=warnings,
            note="Apple does not allow a keyword's match type to be edited in place.",
        )

    def current(self, ctx, item):
        if item["field"] == CREATED:
            return None
        return enum_value(client.get_keyword(item["entity_id"]).status) or "unknown"

    def execute(self, ctx, items, call):
        from apple_ads_mcp.client import WRITE_TIMEOUT

        create, pause = items
        add = AddKeywords()
        method, kwargs = add.request(ctx, [create])
        created = add.observe(ctx, call(method, _request_timeout=WRITE_TIMEOUT, **kwargs), [create])[0]
        if not created.landed:
            # Nothing created, so the old keyword is left exactly as it was.
            return [created, Outcome(False, None, error="not paused: the new keyword was not created")]
        status = KeywordStatusKind()
        try:
            method, kwargs = status.request(ctx, [pause])
            paused = status.observe(ctx, call(method, _request_timeout=WRITE_TIMEOUT, **kwargs), [pause])[0]
        except ToolError as exc:
            from apple_ads_mcp.changes.engine import Engine

            if Engine.may_have_landed(exc):
                # A timeout or 5xx: the pause may have reached Apple and landed.
                # UNKNOWN, as the engine records a whole request that does this.
                paused = Outcome(False, None, unknown=True, error=(
                    f"OUTCOME UNKNOWN: the new keyword was created, but the pause of "
                    f"the old one got no answer ({exc}), so it may have landed. Check "
                    f"the old keyword with get_keyword before retrying."
                ))
            else:
                paused = Outcome(False, None, error=f"the new keyword was created but the old was not paused: {exc}")
        return [created, paused]


class NegativeKeywordStatusBulk(ChangeKind):
    name = "negative_keyword_status"
    preview_title = "Preview pausing or enabling several negative keywords (no write)"
    apply_title = "Apply a negative keyword status change in bulk (CHANGES WHAT TRAFFIC IS EXCLUDED)"
    apply_annotations = APPLY
    bulk = True
    ledger_keys = ()  # its rows are (negative_keyword, status), owned by negative_keyword_pause

    def propose(
        self,
        ctx,
        negative_keyword_ids: Annotated[list[int], Field(description="Negative keyword ids")],
        status: Annotated[str, Field(description="PAUSED to stop excluding, ENABLED to resume")],
    ) -> Proposal:
        """Show what pausing or enabling several negative keywords would do.

        Pausing a negative lets its traffic back in, so it can INCREASE spend:
        each campaign touched is projected at its unused daily budget, once.
        """
        target = parse_choice(status, SETTABLE_STATUSES)
        ids = list(dict.fromkeys(negative_keyword_ids))
        if not ids:
            raise ToolError("negative_keyword_ids is empty; there is nothing to change.")
        single = NegativeKeywordStatusKind()
        items, children, checks = [], [], []
        bounded: dict[Any, Decimal] = {}
        total = Decimal("0")
        for negative_id in ids:
            negative = client.get_negative_keyword(negative_id)
            before = enum_value(negative.status)
            if before == target:
                raise ToolError(f"negative keyword {negative_id} is already {target}; drop it from the list.")
            campaign = ctx.campaign(negative.campaign_id)
            ad_group = ctx.ad_group(negative.ad_group_id)
            path = entity_path(campaign, ad_group, negative.text, keyword_label="NegativeKeyword")
            projected = Decimal("0")
            if target == "PAUSED" and negative.campaign_id not in bounded:
                bounded[negative.campaign_id] = Decimal("0")
                projected, bound = bound_check(*single.enable_bound(ctx, negative.campaign_id))
                bounded[negative.campaign_id] = projected
                checks.extend(bound)
            total += projected
            checks += [
                check_campaign_in_scope(ctx.limits, negative.campaign_id),
                check_known_status("negative_keyword_status_known", before),
            ]
            items.append(make_item(
                entity_type="negative_keyword", entity_id=negative_id, entity_name=negative.text,
                path=path, field="status", before=before, after=target,
                campaign_id=negative.campaign_id, projected=projected,
                campaign_bound=str(bounded.get(negative.campaign_id, Decimal("0"))),
            ))
            children.append(child_preview(
                tool="apply_negative_keyword_status", entity_type="negative_keyword",
                entity_id=negative_id, entity_name=negative.text, path=path, field="status",
                before=before or "unknown", after=target, campaign=campaign,
                entity_serving=enum_value(getattr(campaign, "display_status", None)),
                projected=projected,
            ))
        warnings = []
        if target == "PAUSED":
            warnings.append(
                "Pausing NEGATIVE keywords stops excluding these terms, so traffic and "
                "spend can go UP. Each campaign is projected at its unused daily budget."
            )
        return Proposal(
            items=items, checks=checks, projected=total, children=children,
            warnings=warnings, is_enable=target == "PAUSED",
            note="Undo one item or the whole batch with revert_change.",
        )

    def current(self, ctx, item):
        return enum_value(client.get_negative_keyword(item["entity_id"]).status) or "unknown"

    def landed_projections(self, items, outcomes):
        """Each campaign's bound, once, on the first of its items that LANDED.

        The preview puts the bound on the first item per campaign; if that one
        fails and another in the same campaign lands, the traffic is still back,
        so the bound moves to the item that landed.
        """
        if not any("campaign_bound" in item for item in items):
            return super().landed_projections(items, outcomes)
        counted: set[Any] = set()
        recorded = []
        for item, outcome in zip(items, outcomes):
            value = Decimal("0")
            campaign = item.get("campaign_id")
            if outcome.landed and campaign not in counted:
                counted.add(campaign)
                value = max(Decimal(str(item.get("campaign_bound") or "0")), Decimal("0"))
            recorded.append(value)
        return recorded

    def request(self, ctx, items):
        return "negative_keywords_bulk_update_post", {
            "x_ap_context": context_header(),
            "negative_keyword_update_bulk_request": NegativeKeywordUpdateBulkRequest(
                allow_partial_success=True,
                items=[
                    NegativeKeywordUpdateBulkRequestItem(
                        correlation_id=index,
                        data=BulkNegativeKeywordUpdate(
                            id=int(item["entity_id"]), status=NegativeKeywordStatus(item["after"])
                        ),
                    )
                    for index, item in enumerate(items)
                ],
            ),
        }

    def observe(self, ctx, response, items):
        def read(result, item):
            observed = enum_value(getattr(result, "status", None))
            return Outcome(landed(observed, item["after"]), observed)

        return _bulk_outcomes(response, items, "bulk negative keyword update", read)
