"""3e: creating campaigns and ad groups -- always PAUSED.

Creating spends nothing (projection 0): the budget only counts once the campaign
is enabled through campaign_status, which projects the whole daily budget. Each
costs 3 applies against the session and daily caps, and is marked destructive
because nothing created can be deleted from here -- pausing is the undo.

A campaign carries a legal declaration: `regulation_responses` is a REQUIRED
argument with no default, rendered verbatim in the preview the human approves.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any

from apple_ads_platform import (
    AdGroupCreate,
    AdGroupStatus,
    AdGroupTargetingCreate,
    BidStrategyCreate,
    BidStrategyGoal,
    BidStrategyType,
    BillingEvent,
    CampaignCreate,
    CampaignStatus,
    CampaignTargetingCreate,
    CPAGoalCreate,
    DailyBudgetCreate,
    EligibilityQueryRequest,
    PricingModel,
    PromotedObjectType,
    QueryPagination,
    RegulationResponseCreate,
    RegulationResponseValue,
    RegulationType,
    TargetingDataCreate,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from apple_ads_mcp import client
from apple_ads_mcp.changes.adgroups import AdGroupStatusKind
from apple_ads_mcp.changes.base import (
    APPLY_DESTRUCTIVE,
    CREATED,
    NOTHING,
    ChangeKind,
    Outcome,
    Proposal,
    item as make_item,
)
from apple_ads_mcp.changes.bidding import STRATEGY_GOALS, STRATEGY_TYPES, _parse_when
from apple_ads_mcp.changes.campaigns import CampaignStatusKind
from apple_ads_mcp.changes.common import campaign_currency, name_is_free, parse_choice
from apple_ads_mcp.client import (
    context_header,
    entity_path,
    enum_value,
    eq_filter,
    landed,
    parse_decimal,
    to_money,
    unwrap,
)
from apple_ads_mcp.guardrails import (
    Check,
    check_campaign_in_scope,
    check_ceiling,
    check_currency,
)

PLACEMENTS = {
    "APPSTORE_SEARCH_RESULTS",
    "APPSTORE_SEARCH_TAB",
    "APPSTORE_TODAY_TAB",
    "APPSTORE_PRODUCT_PAGES_BROWSE",
}
REGULATION_TYPES = {"CAC", "CAMPAIGN_SAPIN_LAW", "ORG_SAPIN_LAW"}
REGULATION_VALUES = {
    "AGENT", "FALSE", "FRENCH_BUSINESS", "NOT_AGENT", "NOT_ANSWERED", "NOT_FRENCH_BUSINESS", "TRUE",
}


class RegulationAnswer(BaseModel):
    regulation_type: str = Field(description="CAC, CAMPAIGN_SAPIN_LAW or ORG_SAPIN_LAW")
    response_value: str = Field(
        description="TRUE, FALSE, AGENT, NOT_AGENT, FRENCH_BUSINESS, NOT_FRENCH_BUSINESS or NOT_ANSWERED"
    )


def _created(response: Any, what: str, after: str) -> list[Outcome]:
    entity = unwrap(response, what)
    new_id = getattr(entity, "id", None)
    observed = enum_value(getattr(entity, "status", None))
    return [Outcome(
        new_id is not None and landed(observed, after), observed,
        created_id=str(new_id) if new_id is not None else None,
    )]


def _eligibility_check(adam_id: str, countries: list[str]) -> tuple[Check, list[str]]:
    """Whether the app may be advertised at all; never a silent pass."""
    try:
        rows = unwrap(
            client.call(
                "eligibilities_apps_query_post", x_ap_context=context_header(),
                eligibility_query_request=EligibilityQueryRequest(
                    filters=[eq_filter("adamId", adam_id)],
                    pagination=QueryPagination(page_size=1000, offset=0, fetch_total_count=True),
                ),
            ),
            "app eligibility",
        ) or []
    except ToolError as exc:
        return Check("app_eligible", False, f"could not confirm eligibility: {str(exc)[:160]}"), []
    rows = rows if isinstance(rows, list) else [rows]
    states = {str(enum_value(getattr(r, "state", None))) for r in rows}
    if not rows:
        return Check("app_eligible", False, f"Apple returned no eligibility for app {adam_id}"), []
    if states and all("INELIGIBLE" in s.upper() for s in states):
        return Check("app_eligible", False, f"app {adam_id} is ineligible ({', '.join(sorted(states))})"), []
    covered = set()
    for row in rows:
        value = getattr(row, "country_or_region", None)
        values = value if isinstance(value, list) else ([value] if value else [])
        covered.update(str(c).upper() for c in values)
    missing = [c for c in countries if covered and c not in covered]
    warnings = [f"No eligibility row for {', '.join(missing)}; Apple may refuse those."] if missing else []
    return Check("app_eligible", True, f"app {adam_id}: {', '.join(sorted(states)) or 'eligible'}"), warnings


class CreateCampaign(ChangeKind):
    name = "create_campaign"
    creates_named = ("campaign",)
    preview_title = "Preview creating a campaign, created PAUSED (no write)"
    apply_title = "Create a campaign (created PAUSED; carries a legal declaration; never deleted)"
    apply_annotations = APPLY_DESTRUCTIVE
    cost = 3
    ledger_keys = (("campaign", CREATED),)
    success_note = (
        "Created PAUSED. Add ad groups (create_ad_group) and keywords, then enable "
        "it with preview_campaign_status, which projects its whole daily budget."
    )

    def propose(
        self,
        ctx,
        name: Annotated[str, Field(description="Campaign name")],
        adam_id: Annotated[str, Field(description="The app to promote (adamId, from search_apps)")],
        countries: Annotated[list[str], Field(description='Two-letter codes, e.g. ["US"]')],
        daily_budget: Annotated[str, Field(description='Daily budget as a decimal string, e.g. "50.00"')],
        regulation_responses: Annotated[
            list[RegulationAnswer],
            Field(description="REQUIRED, no default: the legal declarations this campaign "
                  "makes (e.g. France's Sapin law, China's CAC). Pass [] only when none "
                  "apply -- the human approving the preview is answering for them."),
        ],
        supply_placement: Annotated[
            str,
            Field(description="APPSTORE_SEARCH_RESULTS, APPSTORE_SEARCH_TAB, "
                  "APPSTORE_TODAY_TAB or APPSTORE_PRODUCT_PAGES_BROWSE"),
        ] = "APPSTORE_SEARCH_RESULTS",
        billing_event: Annotated[str, Field(description="TAPS or IMPRESSIONS")] = "TAPS",
        start_time: Annotated[str | None, Field(description="Start, e.g. 2026-12-01; omit for now")] = None,
        end_time: Annotated[str | None, Field(description="End, e.g. 2026-12-31; omit for none")] = None,
    ) -> Proposal:
        """Show what creating a campaign would do. Writes nothing.

        Always created PAUSED, so the projection is 0: the budget counts when the
        campaign is enabled. Checks the budget against max_daily_budget, the
        account currency, and the app's eligibility. Costs 3 applies.

        Pass the BASE name only: the configured prefix (<OWNER>-<agent>-) is
        added, and the preview shows the exact name that will be sent.
        """
        stamped = ctx.name_for_create("campaign", name)
        name = stamped.final
        placement = parse_choice(supply_placement, PLACEMENTS, "supply_placement")
        billing = parse_choice(billing_event, {"TAPS", "IMPRESSIONS"}, "billing_event")
        cleaned = [str(c).strip().upper() for c in countries if str(c).strip()]
        if not cleaned or any(len(c) != 2 for c in cleaned):
            raise ToolError('INVALID_REQUEST: countries must be two-letter codes, e.g. ["US"]')
        answers = []
        for answer in regulation_responses:
            answer = answer if isinstance(answer, RegulationAnswer) else RegulationAnswer(**answer)
            answers.append({
                "regulation_type": parse_choice(answer.regulation_type, REGULATION_TYPES, "regulation_type"),
                "response_value": parse_choice(answer.response_value, REGULATION_VALUES, "response_value"),
            })
        budget = parse_decimal(daily_budget, "daily_budget")
        start = _parse_when(start_time, "start_time")
        end = _parse_when(end_time, "end_time")
        if start and end and end <= start:
            raise ToolError("end_time must be after start_time.")
        currency = client.account_currency()
        eligible, warnings = _eligibility_check(adam_id, cleaned)
        checks = [
            check_currency(ctx.limits, currency),
            check_ceiling("max_daily_budget", budget, ctx.limits.max_daily_budget, currency or ""),
            eligible,
            *stamped.checks(),
            name_is_free("campaign", name),
        ]
        if stamped.warning():
            warnings.insert(0, stamped.warning())
        if ctx.limits.allowed_campaign_ids:
            warnings.append(
                "allowed_campaign_ids is pinned, and a new campaign is not in it: this "
                "server will not be able to change it (enable it, add ad groups) until "
                "its id is added."
            )
        if {"FR", "CN"} & set(cleaned) and not answers:
            warnings.append(
                "France and mainland China carry regulation questions (Sapin law, CAC); "
                "Apple is likely to refuse this with no regulation_responses."
            )
        declaration = (
            "; ".join(f"{a['regulation_type']} = {a['response_value']}" for a in answers)
            or "none declared"
        )
        path = f"Campaign {name!r}"
        return Proposal(
            items=[make_item(
                entity_type="campaign", entity_id=None, entity_name=name, path=path,
                field=CREATED, before=NOTHING, after="PAUSED", campaign_id=None,
                adam_id=str(adam_id), countries=cleaned, budget=str(budget), currency=currency,
                placement=placement, billing_event=billing, regulation_responses=answers,
                start_time=start.isoformat() if start else None,
                end_time=end.isoformat() if end else None,
                **stamped.item_fields(ctx.identity),
            )],
            checks=checks, projected=Decimal("0"),
            created_names=[("campaign", name)], naming=stamped.block(),
            warnings=["NOT idempotent: applying this twice creates two campaigns."] + warnings,
            note=f"Regulation responses being declared: {declaration}.",
            entity_type="campaign", entity_id="(new)", entity_name=name, path=path,
            field_name="exists", before="absent",
            after=(
                f"PAUSED campaign for app {adam_id} in {', '.join(cleaned)}, "
                f"{placement}, {budget} {currency or ''}/day, billed on {billing}; "
                f"regulation: {declaration}"
            ),
        )

    def current(self, ctx, item):
        return None

    def request(self, ctx, items):
        item = items[0]
        kwargs: dict[str, Any] = dict(
            ad_account_id=int(client.ad_account()),
            name=item["entity_name"],
            billing_event=BillingEvent(item["billing_event"]),
            promoted_object_type=PromotedObjectType("APPSTORE_APP"),
            promoted_object_id=item["adam_id"],
            status=CampaignStatus("PAUSED"),
            daily_budget=DailyBudgetCreate(value=to_money(item["budget"], item.get("currency"))),
            targeting=CampaignTargetingCreate(
                supply_source=TargetingDataCreate(include=["APPSTORE"]),
                supply_placement=TargetingDataCreate(include=[item["placement"]]),
                country_or_region=TargetingDataCreate(include=list(item["countries"])),
            ),
            regulation_responses=[
                RegulationResponseCreate(
                    regulation_type=RegulationType(a["regulation_type"]),
                    response_value=RegulationResponseValue(a["response_value"]),
                )
                for a in item["regulation_responses"]
            ],
        )
        if item.get("start_time"):
            kwargs["start_time"] = dt.datetime.fromisoformat(item["start_time"])
        if item.get("end_time"):
            kwargs["end_time"] = dt.datetime.fromisoformat(item["end_time"])
        return "campaigns_post", {"x_ap_context": context_header(), "campaign_create": CampaignCreate(**kwargs)}

    def observe(self, ctx, response, items):
        return _created(response, "campaign", items[0]["after"])

    def inverse(self, ctx, entry):
        return CampaignStatusKind().undo_creation(entry)


class CreateAdGroup(ChangeKind):
    name = "create_ad_group"
    creates_named = ("ad_group",)
    preview_title = "Preview creating an ad group, created PAUSED (no write)"
    apply_title = "Create an ad group (created PAUSED; never deleted -- pausing is the undo)"
    apply_annotations = APPLY_DESTRUCTIVE
    cost = 3
    ledger_keys = (("ad_group", CREATED),)
    success_note = "Created PAUSED. Add keywords (add_keywords), then enable it with preview_ad_group_status."

    def propose(
        self,
        ctx,
        campaign_id: Annotated[int, Field(description="Campaign to create it in")],
        name: Annotated[str, Field(description="Ad group name")],
        default_bid: Annotated[str, Field(description='Default bid as a decimal string, e.g. "1.50"')],
        bid_strategy_type: Annotated[str, Field(description="MANUAL_CPT (default) or MAX_CONVERSIONS")] = "MANUAL_CPT",
        bid_strategy_goal: Annotated[str, Field(description="TAP, INSTALL or IMPRESSION")] = "TAP",
        pricing_model: Annotated[str, Field(description="CPT (default) or CPM")] = "CPT",
        cpa_goal: Annotated[str | None, Field(description='Optional CPA goal, e.g. "4.00"')] = None,
        search_match: Annotated[bool, Field(description="Switch Search Match on")] = False,
        devices: Annotated[list[str] | None, Field(description='["IPHONE"], ["IPAD"] or both; omit for both')] = None,
    ) -> Proposal:
        """Show what creating an ad group would do. Writes nothing.

        Always created PAUSED, so the projection is 0. The default bid is checked
        against max_bid and the CPA goal against max_cpa_goal. Costs 3 applies.

        Pass the BASE name only: the configured prefix (<OWNER>-<agent>-) is
        added, and the preview shows the exact name that will be sent.
        """
        stamped = ctx.name_for_create("ad_group", name)
        name = stamped.final
        strategy_type = parse_choice(bid_strategy_type, STRATEGY_TYPES, "bid_strategy_type")
        goal = parse_choice(bid_strategy_goal, STRATEGY_GOALS, "bid_strategy_goal")
        pricing = parse_choice(pricing_model, {"CPT", "CPM"}, "pricing_model")
        bid = parse_decimal(default_bid, "default_bid")
        cpa = parse_decimal(cpa_goal, "cpa_goal") if cpa_goal else None
        device_list = [str(d).strip().upper() for d in (devices or [])]
        if any(d not in ("IPHONE", "IPAD") for d in device_list):
            raise ToolError('INVALID_REQUEST: devices must be "IPHONE" and/or "IPAD"')
        campaign = ctx.campaign(campaign_id)
        currency = campaign_currency(campaign) or client.account_currency()
        checks = [
            check_campaign_in_scope(ctx.limits, campaign_id),
            check_currency(ctx.limits, currency),
            check_ceiling("max_bid", bid, ctx.limits.max_bid, currency or ""),
        ]
        if cpa is not None:
            checks.append(check_ceiling("max_cpa_goal", cpa, ctx.limits.max_cpa_goal, currency or ""))
        checks += [*stamped.checks(), name_is_free("ad_group", name, campaign_id)]
        path = entity_path(campaign) + f" > AdGroup {name!r}"
        return Proposal(
            items=[make_item(
                entity_type="ad_group", entity_id=None, entity_name=name, path=path, field=CREATED,
                before=NOTHING, after="PAUSED", campaign_id=campaign_id, bid=str(bid),
                currency=currency, strategy_type=strategy_type, goal=goal, pricing=pricing,
                cpa=str(cpa) if cpa is not None else None, search_match=search_match,
                devices=device_list, **stamped.item_fields(ctx.identity),
            )],
            checks=checks, projected=Decimal("0"),
            created_names=[("ad_group", name)], naming=stamped.block(),
            warnings=[w for w in (stamped.warning(),) if w]
            + ["NOT idempotent: applying this twice creates two ad groups."],
            entity_type="ad_group", entity_id="(new)", entity_name=name, path=path,
            field_name="exists", before="absent",
            after=(
                f"PAUSED ad group, {strategy_type}/{goal}, default bid {bid} {currency or ''}, "
                f"{pricing}" + (f", CPA goal {cpa}" if cpa is not None else "")
                + f", Search Match {'on' if search_match else 'off'}"
                + (f", devices {', '.join(device_list)}" if device_list else "")
            ),
            campaign=campaign, entity_serving="(created PAUSED)",
        )

    def current(self, ctx, item):
        return None

    def request(self, ctx, items):
        item = items[0]
        kwargs: dict[str, Any] = dict(
            name=item["entity_name"],
            campaign_id=int(item["campaign_id"]),
            pricing_model=PricingModel(item["pricing"]),
            status=AdGroupStatus("PAUSED"),
            automated_keywords_opt_in=bool(item["search_match"]),
            bid_strategy=BidStrategyCreate(
                bid_strategy_type=BidStrategyType(item["strategy_type"]),
                bid_strategy_goal=BidStrategyGoal(item["goal"]),
                bid=to_money(item["bid"], item.get("currency")),
            ),
        )
        if item.get("cpa"):
            kwargs["cpa_cap"] = CPAGoalCreate(value=to_money(item["cpa"], item.get("currency")))
        if item.get("devices"):
            kwargs["targeting"] = AdGroupTargetingCreate(
                device_class=TargetingDataCreate(include=list(item["devices"]))
            )
        return "adgroups_post", {"x_ap_context": context_header(), "ad_group_create": AdGroupCreate(**kwargs)}

    def observe(self, ctx, response, items):
        return _created(response, "ad group", items[0]["after"])

    def inverse(self, ctx, entry):
        return AdGroupStatusKind().undo_creation(entry)
