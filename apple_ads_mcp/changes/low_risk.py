"""Phase 4: the one general tool -- low-risk edits on a closed allowlist.

`preview_low_risk_change(entity_type, entity_id, field, value)` covers edits that
cannot move money or change who sees an ad: renaming a campaign, ad group, ad or
creative, and dismissing one of Apple's recommendations. Everything else --
money, bids, budgets, status, bid strategy, CPA goals, targeting, Search Match,
schedule, shared budgets, billing, regulation -- has a typed pair with its own
bounds and is refused here.

ALLOWLIST is fixed in code and pinned by tests/test_low_risk.py, which also
introspects the SDK update models to prove no allowlisted field is money-typed
or on FORBIDDEN_FIELDS.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from apple_ads_platform import (
    AdGroupUpdate,
    AdUpdate,
    ApplyDailyCapRecommendation,
    ApplyTargetCpaRecommendation,
    CampaignUpdate,
    CreativeUpdate,
    RecommendationQueryRequest,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from apple_ads_mcp import client
from apple_ads_mcp.changes.base import APPLY, ChangeKind, Outcome, Proposal, item as make_item
from apple_ads_mcp.client import context_header, unwrap
from apple_ads_mcp.guardrails import check_campaign_in_scope

#: entity_type -> (get method, put method, update kwarg, update model)
RENAMEABLE = {
    "campaign": ("campaigns_id_get", "campaigns_id_put", "campaign_update", CampaignUpdate),
    "ad_group": ("adgroups_id_get", "adgroups_id_put", "ad_group_update", AdGroupUpdate),
    "ad": ("ads_id_get", "ads_id_put", "ad_update", AdUpdate),
    "creative": ("creatives_id_get", "creatives_id_put", "creative_update", CreativeUpdate),
}

#: recommendation type -> (query method, dismiss method, dismiss kwarg, model)
DISMISSABLE = {
    "daily_budget_recommendation": (
        "query_daily_budget_recommendations", "dismiss_daily_budget_recommendations",
        "apply_daily_cap_recommendation", ApplyDailyCapRecommendation,
    ),
    "target_cpa_recommendation": (
        "query_target_cpa_recommendations", "dismiss_target_cpa_recommendations",
        "apply_target_cpa_recommendation", ApplyTargetCpaRecommendation,
    ),
}

#: The whole surface of this tool: (entity_type, field) pairs. Nothing else.
ALLOWLIST: frozenset[tuple[str, str]] = frozenset(
    {(entity, "name") for entity in RENAMEABLE} | {(rec, "dismiss") for rec in DISMISSABLE}
)

#: Fields this tool must never reach, whatever the allowlist says. A test
#: asserts the allowlist and this set are disjoint, and that no allowlisted
#: field is Money-typed in the SDK model.
FORBIDDEN_FIELDS = frozenset({
    "status", "bid", "bid_strategy", "daily_budget", "cpa_cap", "targeting",
    "automated_keywords_opt_in", "automated_keywords_required", "start_time",
    "end_time", "shared_budgets", "invoice_detail", "regulation_responses",
    "creative_spec",
})


class LowRiskChange(ChangeKind):
    name = "low_risk_change"
    preview_title = "Preview a low-risk edit: a rename, or dismissing a recommendation (no write)"
    apply_title = "Apply a low-risk edit (rename, or dismiss a recommendation)"
    apply_annotations = APPLY
    ledger_keys = tuple((entity, "name") for entity in RENAMEABLE) + tuple(
        (rec, "dismiss") for rec in DISMISSABLE
    )

    def propose(
        self,
        ctx,
        entity_type: Annotated[
            str,
            Field(description="campaign, ad_group, ad, creative, daily_budget_recommendation "
                  "or target_cpa_recommendation"),
        ],
        entity_id: Annotated[str, Field(description="The entity's or recommendation's id")],
        field: Annotated[str, Field(description='"name" for a rename, "dismiss" for a recommendation')],
        value: Annotated[str, Field(description="The new name; ignored for dismiss")] = "",
    ) -> Proposal:
        """Show what a low-risk edit would do. Writes nothing.

        The allowlist is closed: rename a campaign, ad group, ad or creative, or
        dismiss a daily budget or target CPA recommendation. Anything touching
        money, bids, budgets, status or targeting is refused -- use its typed
        preview_* tool. Dismissing has no inverse, and the preview says so.
        """
        entity_type = str(entity_type).strip().lower()
        field = str(field).strip().lower()
        if (entity_type, field) not in ALLOWLIST:
            raise ToolError(
                f"INVALID_REQUEST: ({entity_type}, {field}) is not a low-risk edit. The "
                f"allowlist is: {', '.join(f'{e}.{f}' for e, f in sorted(ALLOWLIST))}. Money, "
                f"bids, budgets, status and targeting each have a typed preview_* tool."
            )
        if field == "name":
            return self._rename(ctx, entity_type, entity_id, value)
        return self._dismiss(ctx, entity_type, entity_id)

    def _fetch(self, entity_type: str, entity_id: Any) -> Any:
        get_method = RENAMEABLE[entity_type][0]
        entity = unwrap(client.call(get_method, id=str(entity_id), x_ap_context=context_header()),
                        entity_type.replace("_", " "))
        if entity is None:
            raise ToolError(f"NOT_FOUND: no {entity_type} {entity_id}")
        return entity

    def _rename(self, ctx, entity_type: str, entity_id: str, value: str) -> Proposal:
        new_name = (value or "").strip()
        if not new_name:
            raise ToolError("INVALID_REQUEST: the new name is empty")
        entity = self._fetch(entity_type, entity_id)
        if entity.name == new_name:
            raise ToolError(f"{entity_type} {entity_id} is already called {new_name!r}.")
        campaign_id = entity.id if entity_type == "campaign" else getattr(entity, "campaign_id", None)
        checks = [] if entity_type == "creative" else [check_campaign_in_scope(ctx.limits, campaign_id)]
        path = f"{entity_type.replace('_', ' ').title().replace(' ', '')} {entity.name!r}"
        return Proposal(
            items=[make_item(
                entity_type=entity_type, entity_id=entity_id, entity_name=entity.name, path=path,
                field="name", before=entity.name, after=new_name, campaign_id=campaign_id,
            )],
            checks=checks, projected=Decimal("0"),
            note="A rename moves no money and changes no delivery. revert_change puts the old name back.",
            entity_type=entity_type, entity_id=str(entity_id), entity_name=entity.name, path=path,
            field_name="name", before=entity.name, after=new_name,
        )

    def _dismiss(self, ctx, entity_type: str, entity_id: str) -> Proposal:
        query_method = DISMISSABLE[entity_type][0]
        rows = unwrap(
            client.call(query_method, x_ap_context=context_header(),
                        recommendation_query_request=RecommendationQueryRequest()),
            "recommendations",
        ) or []
        rec = next((r for r in rows if str(getattr(r, "id", "")) == str(entity_id)), None)
        if rec is None:
            raise ToolError(f"NOT_FOUND: no current {entity_type} {entity_id}")
        campaign_id = getattr(rec, "campaign_id", None)
        path = f"Recommendation {entity_id} (campaign {campaign_id})"
        return Proposal(
            items=[make_item(
                entity_type=entity_type, entity_id=entity_id, entity_name=getattr(rec, "campaign_name", None),
                path=path, field="dismiss", before="open", after="dismissed", campaign_id=campaign_id,
                promoted_object_id=str(getattr(rec, "promoted_object_id", "") or ""),
                promoted_object_type=str(getattr(rec, "promoted_object_type", "") or "APPSTORE_APP"),
            )],
            checks=[check_campaign_in_scope(ctx.limits, campaign_id)], projected=Decimal("0"),
            warnings=["Dismissing has NO inverse: revert_change cannot bring the recommendation back."],
            entity_type=entity_type, entity_id=str(entity_id), entity_name=getattr(rec, "campaign_name", None),
            path=path, field_name="dismiss", before="open", after="dismissed",
        )

    def current(self, ctx, item):
        if item["field"] == "dismiss":
            return None
        return self._fetch(item["entity_type"], item["entity_id"]).name

    def request(self, ctx, items):
        item = items[0]
        if item["field"] == "dismiss":
            _q, method, arg, model = DISMISSABLE[item["entity_type"]]
            return method, {
                "x_ap_context": context_header(),
                arg: [model(id=str(item["entity_id"]), promoted_object_id=item["promoted_object_id"],
                            promoted_object_type=item["promoted_object_type"])],
            }
        _get, method, arg, model = RENAMEABLE[item["entity_type"]]
        return method, {"id": str(item["entity_id"]), "x_ap_context": context_header(),
                        arg: model(name=item["after"])}

    def observe(self, ctx, response, items):
        item = items[0]
        if item["field"] == "dismiss":
            # Apple's answer is the acknowledgement; there is nothing to read back.
            unwrap(response, "dismiss")
            return [Outcome(True, "dismissed")]
        updated = unwrap(response, f"{item['entity_type']} update")
        observed = getattr(updated, "name", None) if updated else None
        return [Outcome(observed == item["after"], observed)]

    def inverse(self, ctx, entry):
        if entry.get("field") == "dismiss":
            raise ValueError("a dismissed recommendation cannot be brought back from here.")
        return self, make_item(
            entity_type=entry["entity_type"], entity_id=entry["entity_id"],
            entity_name=entry.get("after"), path=str(entry.get("path")), field="name",
            before=str(entry["after"]), after=str(entry["before"]), campaign_id=entry.get("campaign_id"),
        )
