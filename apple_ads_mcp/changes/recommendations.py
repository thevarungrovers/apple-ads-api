"""3f: acting on Apple's recommendations -- through the typed kind each amounts to.

`apply_daily_budget_recommendations` and `apply_target_cpa_recommendations` stay
unreachable (FORBIDDEN_METHODS): a one-call "apply Apple's advice" would move
money with no preview, no bound and no ledger line. Instead each preview here
READS the recommendation and builds the ordinary campaign_daily_budget or
ad_group_cpa_goal change from it, so every bound, the staleness check and the
ledger apply exactly as if a human had typed the number.
"""

from __future__ import annotations

from typing import Annotated, Any

from apple_ads_platform import RecommendationFilterCondition, RecommendationQueryRequest
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from apple_ads_mcp import client
from apple_ads_mcp.changes.base import Proposal
from apple_ads_mcp.changes.bidding import AdGroupCpaGoal, cpa_proposal
from apple_ads_mcp.changes.campaigns import CampaignDailyBudget, budget_proposal
from apple_ads_mcp.client import context_header, enum_value, unwrap


def _recommendations(method: str, campaign_id: int) -> list[Any]:
    response = client.call(
        method,
        x_ap_context=context_header(),
        recommendation_query_request=RecommendationQueryRequest(filters=[
            RecommendationFilterCondition(var_field="campaignId", operator="EQUALS", value=[str(campaign_id)])
        ]),
    )
    rows = unwrap(response, "recommendations") or []
    return [r for r in rows if str(getattr(r, "campaign_id", campaign_id)) == str(campaign_id)]


def _pick(rows: list[Any], recommendation_id: str | None, what: str) -> Any:
    if recommendation_id:
        rows = [r for r in rows if str(getattr(r, "id", "")) == str(recommendation_id)]
    if not rows:
        raise ToolError(f"NOT_FOUND: Apple has no {what} recommendation for that campaign right now.")
    return rows[0]


class BudgetRecommendation(CampaignDailyBudget):
    name = "budget_recommendation"
    preview_title = "Preview applying Apple's daily budget recommendation (no write)"
    apply_title = "Apply Apple's recommended daily budget (SPENDS MONEY; normal budget bounds apply)"
    # Its ledger rows ARE campaign daily budget rows, reverted by that kind.
    ledger_keys = ()

    def propose(
        self,
        ctx,
        campaign_id: Annotated[int, Field(description="Apple campaign id")],
        recommendation_id: Annotated[
            str | None, Field(description="A specific recommendation id; omit for the current one")
        ] = None,
    ) -> Proposal:
        """Show what applying Apple's daily budget recommendation would do. Writes nothing.

        Reads the recommendation and previews it as an ordinary
        campaign_daily_budget change: max_daily_budget, max_budget_change_pct and
        the daily total all apply, and a recommendation outside them is blocked
        like any other number.
        """
        rec = _pick(_recommendations("query_daily_budget_recommendations", campaign_id),
                    recommendation_id, "daily budget")
        amount = getattr(getattr(rec, "suggested_daily_budget_amount", None), "amount", None)
        if amount in (None, ""):
            raise ToolError("INVALID: the recommendation carries no suggested budget.")
        return budget_proposal(
            ctx, campaign_id, str(amount), "recommended daily budget",
            extra_note=(
                f"From Apple's recommendation {getattr(rec, 'id', '?')} "
                f"({enum_value(getattr(rec, 'status', None)) or 'status unknown'})."
            ),
        )


class TargetCpaRecommendation(AdGroupCpaGoal):
    name = "target_cpa_recommendation"
    preview_title = "Preview applying Apple's target CPA recommendation to an ad group (no write)"
    apply_title = "Apply Apple's recommended CPA goal (normal CPA goal bounds apply)"
    ledger_keys = ()  # its rows are ad_group cpa_goal rows

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="The ad group whose CPA goal to set")],
        recommendation_id: Annotated[
            str | None, Field(description="A specific recommendation id; omit for the current one")
        ] = None,
    ) -> Proposal:
        """Show what applying Apple's target CPA recommendation would do. Writes nothing.

        Apple recommends per campaign; this applies the campaign's recommended
        target CPA to one ad group as an ordinary ad_group_cpa_goal change, with
        max_cpa_goal and max_cpa_goal_change_pct.
        """
        ad_group = ctx.ad_group(ad_group_id)
        rec = _pick(_recommendations("query_target_cpa_recommendations", ad_group.campaign_id),
                    recommendation_id, "target CPA")
        amount = getattr(getattr(rec, "recommended_target_cpa", None), "amount", None)
        if amount in (None, ""):
            raise ToolError("INVALID: the recommendation carries no target CPA.")
        return cpa_proposal(
            ctx, ad_group_id, str(amount), "recommended target CPA",
            extra_note=f"From Apple's recommendation {getattr(rec, 'id', '?')}.",
        )
