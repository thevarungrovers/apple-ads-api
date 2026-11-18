"""Campaign status and daily budget."""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from apple_ads_platform import CampaignStatus, CampaignUpdate, DailyBudgetUpdate
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from apple_ads_mcp import client
from apple_ads_mcp.changes.base import (
    APPLY_DESTRUCTIVE,
    ChangeKind,
    Outcome,
    Proposal,
    item as make_item,
)
from apple_ads_mcp.changes.common import (
    SETTABLE_STATUSES,
    StatusKind,
    bound_check,
    campaign_budget,
    campaign_enable_bound,
    metrics_block,
    metrics_warning,
    parse_choice,
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
    to_money,
    unwrap,
)
from apple_ads_mcp.guardrails import (
    check_campaign_in_scope,
    check_ceiling,
    check_currency,
    check_known_status,
    check_pct_increase,
)
from apple_ads_mcp.previews import project_budget_change, project_pause


class CampaignStatusKind(StatusKind):
    name = "campaign_status"
    preview_title = "Preview pausing or enabling a WHOLE CAMPAIGN (no write)"
    apply_title = "Apply a campaign pause or enable (STOPS OR STARTS A WHOLE CAMPAIGN)"
    apply_annotations = APPLY_DESTRUCTIVE
    entity_type = "campaign"
    label = "campaign"
    get_method = "campaigns_id_get"
    put_method = "campaigns_id_put"
    update_arg = "campaign_update"
    ledger_keys = (("campaign", "status"),)

    def update_body(self, status):
        return CampaignUpdate(status=CampaignStatus(status))

    def enable_bound(self, ctx, campaign_id):
        return campaign_enable_bound(ctx.campaign(campaign_id))

    def propose(
        self,
        ctx,
        campaign_id: Annotated[int, Field(description="Apple campaign id")],
        status: Annotated[str, Field(description="ENABLED or PAUSED")],
    ) -> Proposal:
        """Show what pausing or enabling an entire campaign would do. Writes nothing.

        The widest change this server can make. Everything under the campaign
        stops or starts.
        """
        target = parse_choice(status, SETTABLE_STATUSES)
        campaign = ctx.campaign(campaign_id)
        before = enum_value(campaign.status)
        if before == target:
            raise ToolError(f"campaign {campaign_id} is already {target}; nothing to change.")
        spend, taps, impressions, installs, metrics_error = ctx.metrics_7d(
            "apps_campaign_reports", "campaignId", campaign_id
        )
        bound_checks = []
        if target == "PAUSED":
            projected = project_pause(spend)
        else:
            projected, bound_checks = bound_check(*campaign_enable_bound(campaign))
        entity_serving = enum_value(campaign.display_status)
        warnings = serving_warning(entity_serving, "campaign") + metrics_warning(metrics_error)
        warnings.append("This stops or starts EVERY ad group and keyword in the campaign.")
        path = entity_path(campaign)
        return Proposal(
            items=[
                make_item(
                    entity_type="campaign",
                    entity_id=campaign_id,
                    entity_name=campaign.name,
                    path=path,
                    field="status",
                    before=before,
                    after=target,
                    campaign_id=campaign_id,
                    projected=projected,
                )
            ],
            checks=[
                check_campaign_in_scope(ctx.limits, campaign_id),
                check_known_status("campaign_status_known", before),
                *bound_checks,
            ],
            projected=projected,
            is_enable=target == "ENABLED",
            warnings=warnings,
            note=(
                "Reversible by re-enabling, but a paused campaign loses delivery "
                "for as long as it is paused, and that time is not recoverable."
            ),
            entity_type="campaign",
            entity_id=str(campaign_id),
            entity_name=campaign.name,
            path=path,
            field_name="status",
            before=before or "unknown",
            after=target,
            campaign=campaign,
            entity_serving=entity_serving,
            metrics=metrics_block(spend, taps, impressions, installs),
        )


class CampaignDailyBudget(ChangeKind):
    name = "campaign_daily_budget"
    preview_title = "Preview a campaign daily budget change (no write)"
    apply_title = "Apply a campaign daily budget change (SPENDS MONEY)"
    apply_annotations = APPLY_DESTRUCTIVE
    ledger_keys = (("campaign", "daily_budget"),)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[int, Field(description="Apple campaign id")],
        new_daily_budget: Annotated[
            str, Field(description='New daily budget as a decimal string, e.g. "120.00"')
        ],
    ) -> Proposal:
        """Show what changing a campaign's daily budget would do. Writes nothing.

        The budget delta is EXACT, not an estimate: it is the spend cap itself
        moving, not a guess about behaviour.
        """
        return budget_proposal(ctx, campaign_id, new_daily_budget, "new_daily_budget")

    def current(self, ctx, item):
        current = money_amount(campaign_budget(client.get_campaign(int(item["entity_id"]))))
        return str(current) if current is not None else "none"

    def request(self, ctx, items):
        item = items[0]
        return "campaigns_id_put", {
            "id": str(item["entity_id"]),
            "x_ap_context": context_header(),
            "campaign_update": CampaignUpdate(
                daily_budget=DailyBudgetUpdate(value=to_money(item["after"], item.get("currency")))
            ),
        }

    def observe(self, ctx, response, items):
        updated = unwrap(response, "campaign update")
        observed_money = campaign_budget(updated) if updated else None
        observed = format_money(observed_money) if observed_money else None
        return [Outcome(landed(observed, items[0]["after"]), observed)]

    def inverse(self, ctx, entry):
        campaign = client.get_campaign(int(entry["entity_id"]))
        return self, make_item(
            entity_type="campaign",
            entity_id=entry["entity_id"],
            entity_name=entry.get("entity_name"),
            path=str(entry.get("path")),
            field="daily_budget",
            before=str(entry["after"]),
            after=str(entry["before"]),
            campaign_id=entry.get("campaign_id"),
            currency=money_currency(campaign_budget(campaign)),
        )


def budget_proposal(ctx, campaign_id: int, raw_budget: str, label: str, extra_note: str = "") -> Proposal:
    """The campaign_daily_budget preview, shared with the budget recommendation."""
    after_num = parse_decimal(raw_budget, label)
    campaign = ctx.campaign(campaign_id)
    current = campaign_budget(campaign)
    before_num = money_amount(current)
    currency = money_currency(current)
    spend, taps, impressions, installs, metrics_error = ctx.metrics_7d(
        "apps_campaign_reports", "campaignId", campaign_id
    )
    projected = project_budget_change(before_num, after_num)
    entity_serving = enum_value(campaign.display_status)
    warnings = serving_warning(entity_serving, "campaign") + metrics_warning(metrics_error)
    if before_num is not None and after_num < before_num:
        warnings.append(
            "Lowering a daily budget can stop delivery part-way through today "
            "if the campaign has already spent more than the new cap."
        )
    path = entity_path(campaign)
    return Proposal(
        items=[
            make_item(
                entity_type="campaign",
                entity_id=campaign_id,
                entity_name=campaign.name,
                path=path,
                field="daily_budget",
                before=str(before_num) if before_num is not None else None,
                after=str(after_num),
                campaign_id=campaign_id,
                projected=projected,
                currency=currency,
            )
        ],
        checks=[
            check_campaign_in_scope(ctx.limits, campaign_id),
            check_currency(ctx.limits, currency),
            check_ceiling("max_daily_budget", after_num, ctx.limits.max_daily_budget, currency or ""),
            check_pct_increase(
                "max_budget_change_pct", before_num, after_num, ctx.limits.max_budget_change_pct
            ),
            check_known_status("campaign_status_known", enum_value(campaign.status)),
        ],
        projected=projected,
        warnings=warnings,
        note=(
            "projected_daily_spend_delta here is the exact change in the cap. "
            "Actual spend depends on whether the campaign was hitting the old "
            "cap at all -- compare it with the last 7 days' spend."
            + (f" {extra_note}" if extra_note else "")
        ),
        entity_type="campaign",
        entity_id=str(campaign_id),
        entity_name=campaign.name,
        path=path,
        field_name="daily_budget",
        before=format_money(current),
        after=f"{after_num} {currency or ''}".strip(),
        before_num=before_num,
        after_num=after_num,
        campaign=campaign,
        entity_serving=entity_serving,
        metrics=metrics_block(spend, taps, impressions, installs),
    )
