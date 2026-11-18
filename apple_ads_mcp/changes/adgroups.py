"""Ad group status and default bid."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from apple_ads_platform import AdGroupStatus, AdGroupUpdate
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from apple_ads_mcp import client
from apple_ads_mcp.changes.base import APPLY, ChangeKind, Outcome, Proposal, item as make_item
from apple_ads_mcp.changes.common import (
    SETTABLE_STATUSES,
    StatusKind,
    ad_group_bid,
    bid_strategy_goal,
    bid_strategy_type,
    bid_strategy_update,
    bound_check,
    campaign_currency,
    default_bid_blast_radius,
    metrics_block,
    metrics_warning,
    parse_choice,
    refuse_if_moved,
    serving_warning,
)
from apple_ads_mcp.client import (
    context_header,
    entity_path,
    enum_value,
    format_money,
    landed,
    money_amount,
    money_currency,
    parse_decimal,
    unwrap,
)
from apple_ads_mcp.guardrails import (
    check_bid_settable,
    check_campaign_in_scope,
    check_ceiling,
    check_currency,
    check_known_status,
    check_pct_increase,
)
from apple_ads_mcp.previews import project_bid_change, project_pause


class AdGroupStatusKind(StatusKind):
    name = "ad_group_status"
    preview_title = "Preview pausing or enabling an ad group (no write)"
    apply_title = "Apply an ad group pause or enable (AFFECTS EVERY KEYWORD IN IT)"
    apply_annotations = APPLY
    entity_type = "ad_group"
    label = "ad group"
    get_method = "adgroups_id_get"
    put_method = "adgroups_id_put"
    update_arg = "ad_group_update"
    ledger_keys = (("ad_group", "status"),)

    def update_body(self, status):
        return AdGroupUpdate(status=AdGroupStatus(status))

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> Proposal:
        """Show what pausing or enabling an ad group would do. Writes nothing.

        An ad group carries every keyword under it, so this is a much wider
        change than pausing one keyword. The 7-day figures are the ad group's
        total, which is the number that matters.
        """
        target = parse_choice(status, SETTABLE_STATUSES)
        ad_group = ctx.ad_group(ad_group_id)
        campaign = ctx.campaign(ad_group.campaign_id)
        before = enum_value(ad_group.status)
        if before == target:
            raise ToolError(f"ad group {ad_group_id} is already {target}; nothing to change.")
        spend, taps, impressions, installs, metrics_error = ctx.metrics_7d(
            "apps_ad_group_reports", "adGroupId", ad_group_id, ad_group.campaign_id
        )
        bound_checks = []
        if target == "PAUSED":
            projected = project_pause(spend)
        else:
            projected, bound_checks = bound_check(*self.enable_bound(ctx, ad_group.campaign_id))
        entity_serving = enum_value(ad_group.display_status)
        warnings = serving_warning(entity_serving, "ad group") + metrics_warning(metrics_error)
        warnings.append("This affects EVERY keyword in the ad group, not one of them.")
        path = entity_path(campaign, ad_group)
        return Proposal(
            items=[
                make_item(
                    entity_type="ad_group",
                    entity_id=ad_group_id,
                    entity_name=ad_group.name,
                    path=path,
                    field="status",
                    before=before,
                    after=target,
                    campaign_id=ad_group.campaign_id,
                    projected=projected,
                )
            ],
            checks=[
                check_campaign_in_scope(ctx.limits, ad_group.campaign_id),
                check_known_status("ad_group_status_known", before),
                *bound_checks,
            ],
            projected=projected,
            is_enable=target == "ENABLED",
            warnings=warnings,
            note="Reversible: apply the opposite status to undo, or use revert_change.",
            entity_type="ad_group",
            entity_id=str(ad_group_id),
            entity_name=ad_group.name,
            path=path,
            field_name="status",
            before=before or "unknown",
            after=target,
            campaign=campaign,
            entity_serving=entity_serving,
            metrics=metrics_block(spend, taps, impressions, installs),
        )


class AdGroupDefaultBid(ChangeKind):
    name = "ad_group_default_bid"
    preview_title = "Preview an ad group default bid change (no write)"
    apply_title = "Apply an ad group default bid change (CHANGES WHAT THIS AD GROUP COSTS)"
    apply_annotations = APPLY
    ledger_keys = (("ad_group", "default_bid"),)
    failure_note = (
        "NOT APPLIED. Apple returned success and left the default bid alone. "
        "That usually means the ad group is on an automated bid strategy "
        "(MAX_CONVERSIONS / MAX_ENGAGEMENTS), where Apple sets the bids and a "
        "written one is accepted and ignored. Recorded as failed, so "
        "revert_change will refuse it -- there is nothing to undo."
    )

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        new_bid: Annotated[
            str,
            Field(
                description='New default bid as a decimal string in major units, e.g. "3.00"'
            ),
        ],
    ) -> Proposal:
        """Show what changing an ad group's default bid would do. Writes nothing.

        The default bid is what every keyword in the ad group pays unless it
        carries a bid of its own -- and in a Search Tab or Search Match ad group,
        which has no keywords at all, it is the ONLY bid. The blast radius is
        therefore wider than a keyword bid, so the warnings say how many keywords
        this actually reprices and how many override it.
        """
        after_num = parse_decimal(new_bid, "new_bid")
        ad_group = ctx.ad_group(ad_group_id)
        campaign = ctx.campaign(ad_group.campaign_id)
        current_bid = ad_group_bid(ad_group)
        before_num = money_amount(current_bid)
        # An ad group with no bid set yet has no currency to borrow, so fall back
        # to the campaign's budget currency rather than sending a null one.
        currency = money_currency(current_bid) or campaign_currency(campaign)
        spend, taps, impressions, installs, metrics_error = ctx.metrics_7d(
            "apps_ad_group_reports", "adGroupId", ad_group_id, ad_group.campaign_id
        )
        projected = project_bid_change(taps, before_num, after_num)
        entity_serving = enum_value(ad_group.display_status)
        warnings = serving_warning(entity_serving, "ad group") + metrics_warning(metrics_error)
        warnings.extend(default_bid_blast_radius(ad_group_id))
        if before_num is None:
            warnings.append(
                "This ad group reports no default bid today, so there is no previous "
                "value to put back and revert_change will refuse this entry. Note the "
                "current state somewhere before approving."
            )
        path = entity_path(campaign, ad_group)
        return Proposal(
            items=[
                make_item(
                    entity_type="ad_group",
                    entity_id=ad_group_id,
                    entity_name=ad_group.name,
                    path=path,
                    field="default_bid",
                    before=str(before_num) if before_num is not None else None,
                    after=str(after_num),
                    campaign_id=ad_group.campaign_id,
                    projected=projected,
                    currency=currency,
                    bid_strategy_type=bid_strategy_type(ad_group),
                    bid_strategy_goal=bid_strategy_goal(ad_group),
                )
            ],
            checks=[
                check_campaign_in_scope(ctx.limits, ad_group.campaign_id),
                check_currency(ctx.limits, currency),
                check_ceiling("max_bid", after_num, ctx.limits.max_bid, currency or ""),
                check_pct_increase(
                    "max_bid_increase_pct", before_num, after_num, ctx.limits.max_bid_increase_pct
                ),
                check_known_status("ad_group_status_known", enum_value(ad_group.status)),
                check_bid_settable(bid_strategy_type(ad_group), "ad group default bid"),
            ],
            projected=projected,
            warnings=warnings,
            note=(
                "The default bid lives at bidStrategy.bid; the write sends this ad "
                "group's existing bidStrategyType and bidStrategyGoal back unchanged "
                "alongside it, so the bidding model does not move. "
                "projected_daily_spend_delta is an UPPER BOUND: it assumes tap volume "
                "is unchanged, which a bid change is precisely intended to alter. Use "
                "it to catch a 100x typo, not as a forecast."
            ),
            entity_type="ad_group",
            entity_id=str(ad_group_id),
            entity_name=ad_group.name,
            path=path,
            field_name="default_bid",
            before=format_money(current_bid),
            after=f"{after_num} {currency or ''}".strip(),
            before_num=before_num,
            after_num=after_num,
            campaign=campaign,
            entity_serving=entity_serving,
            metrics=metrics_block(spend, taps, impressions, installs),
        )

    def current(self, ctx, item):
        ad_group = client.get_ad_group(int(item["entity_id"]))
        # The write echoes the PREVIEW-time strategy back alongside the bid, so a
        # strategy moved since then (in the UI, say) would be silently undone.
        refuse_if_moved(
            f"{bid_strategy_type(ad_group) or 'unknown'}/{bid_strategy_goal(ad_group) or '-'}",
            f"{item.get('bid_strategy_type') or 'unknown'}/{item.get('bid_strategy_goal') or '-'}",
            f"ad group {item['entity_id']} bid strategy",
        )
        current = money_amount(ad_group_bid(ad_group))
        return str(current) if current is not None else "none"

    def request(self, ctx, items):
        item = items[0]
        return "adgroups_id_put", {
            "id": str(item["entity_id"]),
            "x_ap_context": context_header(),
            "ad_group_update": AdGroupUpdate(
                bid_strategy=bid_strategy_update(
                    item["after"],
                    item.get("currency"),
                    item.get("bid_strategy_type"),
                    item.get("bid_strategy_goal"),
                )
            ),
        }

    def observe(self, ctx, response, items):
        updated = unwrap(response, "ad group update")
        observed = format_money(ad_group_bid(updated)) if updated else None
        return [Outcome(landed(observed, items[0]["after"]), observed)]

    def inverse(self, ctx, entry):
        ad_group = client.get_ad_group(int(entry["entity_id"]))
        return self, make_item(
            entity_type="ad_group",
            entity_id=entry["entity_id"],
            entity_name=entry.get("entity_name"),
            path=str(entry.get("path")),
            field="default_bid",
            before=str(entry["after"]),
            after=str(entry["before"]),
            campaign_id=entry.get("campaign_id"),
            currency=money_currency(ad_group_bid(ad_group)),
            bid_strategy_type=bid_strategy_type(ad_group),
            bid_strategy_goal=bid_strategy_goal(ad_group),
        )
