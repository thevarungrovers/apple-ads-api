"""3b: bid strategies, CPA goals and campaign schedules.

A bid STRATEGY change is not a number moving -- it hands the bids to Apple, or
takes them back -- so it is bounded like an enable: by the campaign's unused
daily budget, counted against the daily and session totals rather than the
per-change limit. A CPA goal and a schedule are bounded by what they can let the
campaign spend.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any

from apple_ads_platform import (
    AdGroupUpdate,
    BidStrategyGoal,
    BidStrategyType,
    BidStrategyUpdate,
    CampaignUpdate,
    CPAGoalUpdate,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

from apple_ads_mcp import client
from apple_ads_mcp.changes.base import (
    APPLY,
    APPLY_DESTRUCTIVE,
    ChangeKind,
    Outcome,
    Proposal,
    item as make_item,
)
from apple_ads_mcp.changes.common import (
    ad_group_bid,
    bid_strategy_goal as strategy_goal_of,
    bid_strategy_type as strategy_type_of,
    bound_check,
    campaign_currency,
    campaign_enable_bound,
    campaign_headroom,
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

STRATEGY_TYPES = {"MANUAL_CPT", "MAX_CONVERSIONS", "MANUAL_CPM", "MAX_ENGAGEMENTS"}
STRATEGY_GOALS = {"TAP", "INSTALL", "IMPRESSION"}
AUTOMATED = {"MAX_CONVERSIONS", "MAX_ENGAGEMENTS"}


def strategy_text(strategy_type: str | None, goal: str | None) -> str:
    """How a strategy is written into before/after: `MANUAL_CPT/TAP`."""
    return f"{strategy_type or 'unknown'}/{goal or '-'}"


def parse_strategy(text: str) -> tuple[str | None, str | None]:
    kind, _, goal = str(text).partition("/")
    return (None if kind in ("", "unknown") else kind), (None if goal in ("", "-") else goal)


class _StrategyKind(ChangeKind):
    """A bid strategy on an ad group or a campaign. Subclasses say which."""

    entity_type = ""

    def fetch(self, entity_id: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def body(self, strategy: BidStrategyUpdate) -> tuple[str, str, Any]:  # pragma: no cover
        raise NotImplementedError

    def current(self, ctx, item):
        entity = self.fetch(item["entity_id"])
        return strategy_text(strategy_type_of(entity), strategy_goal_of(entity))

    def request(self, ctx, items):
        item = items[0]
        strategy_type, goal = parse_strategy(item["after"])
        kwargs: dict[str, Any] = {}
        if strategy_type:
            kwargs["bid_strategy_type"] = BidStrategyType(strategy_type)
        if goal:
            kwargs["bid_strategy_goal"] = BidStrategyGoal(goal)
        if item.get("bid"):
            kwargs["bid"] = to_money(item["bid"], item.get("currency"))
        method, arg, update = self.body(BidStrategyUpdate(**kwargs))
        return method, {"id": str(item["entity_id"]), "x_ap_context": context_header(), arg: update}

    def observe(self, ctx, response, items):
        updated = unwrap(response, f"{self.entity_type} update")
        observed = (
            strategy_text(strategy_type_of(updated), strategy_goal_of(updated)) if updated else None
        )
        return [Outcome(observed == items[0]["after"], observed)]

    def inverse(self, ctx, entry):
        entity = self.fetch(entry["entity_id"])
        bid = ad_group_bid(entity)
        return self, make_item(
            entity_type=self.entity_type, entity_id=entry["entity_id"],
            entity_name=entry.get("entity_name"), path=str(entry.get("path")),
            field="bid_strategy", before=str(entry["after"]), after=str(entry["before"]),
            campaign_id=entry.get("campaign_id"),
            bid=str(money_amount(bid)) if money_amount(bid) is not None else None,
            currency=money_currency(bid),
        )

    def revert_projection(self, ctx, entry, item):
        bound, reason = campaign_headroom(ctx, ctx.campaign(item.get("campaign_id")))
        if bound is None:
            raise ValueError(f"cannot revert: {reason}.")
        return bound, True


class AdGroupBidStrategy(_StrategyKind):
    name = "ad_group_bid_strategy"
    preview_title = "Preview an ad group's bid strategy change (no write)"
    apply_title = "Apply an ad group bid strategy change (HANDS BIDS TO APPLE, OR TAKES THEM BACK)"
    apply_annotations = APPLY
    entity_type = "ad_group"
    ledger_keys = (("ad_group", "bid_strategy"),)

    def fetch(self, entity_id):
        return client.get_ad_group(int(entity_id))

    def body(self, strategy):
        return "adgroups_id_put", "ad_group_update", AdGroupUpdate(bid_strategy=strategy)

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        bid_strategy_type: Annotated[
            str, Field(description="MANUAL_CPT, MAX_CONVERSIONS, MANUAL_CPM or MAX_ENGAGEMENTS")
        ],
        bid_strategy_goal: Annotated[str | None, Field(description="TAP, INSTALL or IMPRESSION")] = None,
    ) -> Proposal:
        """Show what changing an ad group's bid strategy would do. Writes nothing.

        Moving to an automated strategy (MAX_CONVERSIONS / MAX_ENGAGEMENTS) hands
        the bids to Apple: every keyword bid and the default bid stop being used.
        Moving back to MANUAL makes them count again. Projected at the campaign's
        unused daily budget -- what Apple's bidding could add -- and counted
        against the daily total like an enable.
        """
        target_type = parse_choice(bid_strategy_type, STRATEGY_TYPES, "bid_strategy_type")
        ad_group = ctx.ad_group(ad_group_id)
        campaign = ctx.campaign(ad_group.campaign_id)
        current_type, current_goal = strategy_type_of(ad_group), strategy_goal_of(ad_group)
        target_goal = (
            parse_choice(bid_strategy_goal, STRATEGY_GOALS, "bid_strategy_goal")
            if bid_strategy_goal else current_goal
        )
        before = strategy_text(current_type, current_goal)
        after = strategy_text(target_type, target_goal)
        if before == after:
            raise ToolError(f"ad group {ad_group_id} is already {after}; nothing to change.")
        projected, bound = bound_check(*campaign_headroom(ctx, campaign))
        bid = ad_group_bid(ad_group)
        warnings = serving_warning(enum_value(ad_group.display_status), "ad group")
        if target_type in AUTOMATED and current_type not in AUTOMATED:
            warnings.append(
                "Apple will set the bids from now on: this ad group's default bid and "
                "every keyword bid in it stop being used (they are kept, not deleted)."
            )
        if current_type in AUTOMATED and target_type not in AUTOMATED:
            warnings.append(
                f"Bids you set start to apply again: the default bid "
                f"({format_money(bid)}) and every keyword's own bid. Check them first."
            )
        path = entity_path(campaign, ad_group)
        return Proposal(
            items=[make_item(
                entity_type="ad_group", entity_id=ad_group_id, entity_name=ad_group.name,
                path=path, field="bid_strategy", before=before, after=after,
                campaign_id=ad_group.campaign_id, projected=projected,
                bid=str(money_amount(bid)) if money_amount(bid) is not None else None,
                currency=money_currency(bid) or campaign_currency(campaign),
            )],
            checks=[
                check_campaign_in_scope(ctx.limits, ad_group.campaign_id),
                check_known_status("ad_group_status_known", enum_value(ad_group.status)),
                *bound,
            ],
            projected=projected, is_enable=True, warnings=warnings,
            note="Reversible with revert_change, which puts the previous strategy back.",
            entity_type="ad_group", entity_id=str(ad_group_id), entity_name=ad_group.name,
            path=path, field_name="bid_strategy", before=before, after=after, campaign=campaign,
            entity_serving=enum_value(ad_group.display_status),
        )


class CampaignBidStrategy(_StrategyKind):
    name = "campaign_bid_strategy"
    preview_title = "Preview a campaign's bid strategy change (no write)"
    apply_title = "Apply a campaign bid strategy change (AFFECTS EVERY AD GROUP IN IT)"
    apply_annotations = APPLY_DESTRUCTIVE
    entity_type = "campaign"
    ledger_keys = (("campaign", "bid_strategy"),)

    def fetch(self, entity_id):
        return client.get_campaign(int(entity_id))

    def body(self, strategy):
        return "campaigns_id_put", "campaign_update", CampaignUpdate(bid_strategy=strategy)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[int, Field(description="Apple campaign id")],
        bid_strategy_type: Annotated[
            str, Field(description="MANUAL_CPT, MAX_CONVERSIONS, MANUAL_CPM or MAX_ENGAGEMENTS")
        ],
        bid_strategy_goal: Annotated[str | None, Field(description="TAP, INSTALL or IMPRESSION")] = None,
    ) -> Proposal:
        """Show what changing a campaign-level bid strategy would do. Writes nothing.

        Only some campaigns carry a strategy of their own; Apple refuses the
        write where it does not allow one, and the apply then reports that.
        Projected at the campaign's whole daily budget and counted like an enable.
        """
        target_type = parse_choice(bid_strategy_type, STRATEGY_TYPES, "bid_strategy_type")
        campaign = ctx.campaign(campaign_id)
        current_type, current_goal = strategy_type_of(campaign), strategy_goal_of(campaign)
        target_goal = (
            parse_choice(bid_strategy_goal, STRATEGY_GOALS, "bid_strategy_goal")
            if bid_strategy_goal else current_goal
        )
        before, after = strategy_text(current_type, current_goal), strategy_text(target_type, target_goal)
        if before == after:
            raise ToolError(f"campaign {campaign_id} is already {after}; nothing to change.")
        projected, bound = bound_check(*campaign_enable_bound(campaign))
        warnings = serving_warning(enum_value(campaign.display_status), "campaign")
        if current_type is None:
            warnings.append(
                "This campaign reports no bid strategy of its own; Apple may refuse to "
                "set one. revert_change cannot put 'none' back."
            )
        path = entity_path(campaign)
        return Proposal(
            items=[make_item(
                entity_type="campaign", entity_id=campaign_id, entity_name=campaign.name,
                path=path, field="bid_strategy", before=before, after=after,
                campaign_id=campaign_id, projected=projected,
            )],
            checks=[
                check_campaign_in_scope(ctx.limits, campaign_id),
                check_known_status("campaign_status_known", enum_value(campaign.status)),
                *bound,
            ],
            projected=projected, is_enable=True, warnings=warnings,
            entity_type="campaign", entity_id=str(campaign_id), entity_name=campaign.name,
            path=path, field_name="bid_strategy", before=before, after=after, campaign=campaign,
            entity_serving=enum_value(campaign.display_status),
        )

    def inverse(self, ctx, entry):
        if parse_strategy(str(entry["before"]))[0] is None:
            raise ValueError("the campaign had no bid strategy before, and 'none' cannot be written back.")
        return super().inverse(ctx, entry)


class AdGroupCpaGoal(ChangeKind):
    name = "ad_group_cpa_goal"
    preview_title = "Preview an ad group's CPA goal change (no write)"
    apply_title = "Apply an ad group CPA goal change (CHANGES WHAT APPLE MAY PAY PER INSTALL)"
    apply_annotations = APPLY
    ledger_keys = (("ad_group", "cpa_goal"),)

    def propose(
        self,
        ctx,
        ad_group_id: Annotated[int, Field(description="Apple ad group id")],
        cpa_goal: Annotated[
            str, Field(description='New CPA goal as a decimal string in major units, e.g. "4.50"')
        ],
    ) -> Proposal:
        """Show what changing an ad group's CPA goal (cpaCap) would do. Writes nothing.

        A higher goal lets Apple pay more per install. Projected as the 7-day
        average daily spend scaled by the increase -- an upper bound that assumes
        volume holds -- and checked against max_cpa_goal and
        max_cpa_goal_change_pct.
        """
        return cpa_proposal(ctx, ad_group_id, cpa_goal, "cpa_goal")

    def current(self, ctx, item):
        ad_group = client.get_ad_group(int(item["entity_id"]))
        value = money_amount(getattr(getattr(ad_group, "cpa_cap", None), "value", None))
        return str(value) if value is not None else "none"

    def request(self, ctx, items):
        item = items[0]
        return "adgroups_id_put", {
            "id": str(item["entity_id"]),
            "x_ap_context": context_header(),
            "ad_group_update": AdGroupUpdate(
                cpa_cap=CPAGoalUpdate(value=to_money(item["after"], item.get("currency")))
            ),
        }

    def observe(self, ctx, response, items):
        updated = unwrap(response, "ad group update")
        value = getattr(getattr(updated, "cpa_cap", None), "value", None) if updated else None
        observed = format_money(value) if value else None
        return [Outcome(landed(observed, items[0]["after"]), observed)]

    def inverse(self, ctx, entry):
        ad_group = client.get_ad_group(int(entry["entity_id"]))
        value = getattr(getattr(ad_group, "cpa_cap", None), "value", None)
        return self, make_item(
            entity_type="ad_group", entity_id=entry["entity_id"],
            entity_name=entry.get("entity_name"), path=str(entry.get("path")),
            field="cpa_goal", before=str(entry["after"]), after=str(entry["before"]),
            campaign_id=entry.get("campaign_id"), currency=money_currency(value),
        )


def cpa_proposal(ctx, ad_group_id: int, raw: str, label: str, extra_note: str = "") -> Proposal:
    """The ad_group_cpa_goal preview, shared with the target CPA recommendation."""
    after_num = parse_decimal(raw, label)
    ad_group = ctx.ad_group(ad_group_id)
    campaign = ctx.campaign(ad_group.campaign_id)
    current = getattr(getattr(ad_group, "cpa_cap", None), "value", None)
    before_num = money_amount(current)
    currency = money_currency(current) or campaign_currency(campaign)
    spend, taps, impressions, installs, metrics_error = ctx.metrics_7d(
        "apps_ad_group_reports", "adGroupId", ad_group_id, ad_group.campaign_id
    )
    daily = spend / Decimal(7)
    if before_num and after_num > before_num:
        projected = (daily * (after_num - before_num) / before_num).quantize(Decimal("0.01"))
    else:
        projected = Decimal("0")
    warnings = serving_warning(enum_value(ad_group.display_status), "ad group") + metrics_warning(metrics_error)
    if strategy_type_of(ad_group) not in AUTOMATED:
        warnings.append(
            f"This ad group is on {strategy_type_of(ad_group)}: a CPA goal only steers "
            "an automated (MAX_CONVERSIONS) strategy."
        )
    if before_num is None:
        warnings.append("No CPA goal is set today, so revert_change will refuse this entry.")
    path = entity_path(campaign, ad_group)
    return Proposal(
        items=[make_item(
            entity_type="ad_group", entity_id=ad_group_id, entity_name=ad_group.name, path=path,
            field="cpa_goal", before=str(before_num) if before_num is not None else None,
            after=str(after_num), campaign_id=ad_group.campaign_id, projected=projected,
            currency=currency,
        )],
        checks=[
            check_campaign_in_scope(ctx.limits, ad_group.campaign_id),
            check_currency(ctx.limits, currency),
            check_ceiling("max_cpa_goal", after_num, ctx.limits.max_cpa_goal, currency or ""),
            check_pct_increase(
                "max_cpa_goal_change_pct", before_num, after_num, ctx.limits.max_cpa_goal_change_pct
            ),
            check_known_status("ad_group_status_known", enum_value(ad_group.status)),
        ],
        projected=projected, warnings=warnings,
        note="projected_daily_spend_delta is an UPPER BOUND." + (f" {extra_note}" if extra_note else ""),
        entity_type="ad_group", entity_id=str(ad_group_id), entity_name=ad_group.name, path=path,
        field_name="cpa_goal", before=format_money(current), after=f"{after_num} {currency or ''}".strip(),
        before_num=before_num, after_num=after_num, campaign=campaign,
        entity_serving=enum_value(ad_group.display_status),
        metrics=metrics_block(spend, taps, impressions, installs),
    )


# --- campaign schedule -----------------------------------------------------------


def _parse_when(raw: str | None, field: str) -> dt.datetime | None:
    if raw in (None, ""):
        return None
    text = str(raw).strip()
    try:
        if len(text) == 10:
            return dt.datetime.combine(dt.date.fromisoformat(text), dt.time.min)
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        raise ToolError(f"INVALID_REQUEST: {field}={raw!r} is not a date like 2026-12-01") from None


def _when_text(value: dt.datetime | None) -> str:
    return value.isoformat(timespec="minutes") if value else "none"


def schedule_text(start: dt.datetime | None, end: dt.datetime | None) -> str:
    return f"{_when_text(start)} -> {_when_text(end)}"


def parse_schedule(text: str) -> tuple[dt.datetime | None, dt.datetime | None]:
    start, _, end = str(text).partition(" -> ")
    convert = (lambda v: None if v in ("", "none") else dt.datetime.fromisoformat(v))
    return convert(start), convert(end)


class CampaignSchedule(ChangeKind):
    name = "campaign_schedule"
    preview_title = "Preview a campaign's start/end time change (no write)"
    apply_title = "Apply a campaign schedule change (CHANGES WHEN IT SPENDS)"
    apply_annotations = APPLY_DESTRUCTIVE
    ledger_keys = (("campaign", "schedule"),)

    def propose(
        self,
        ctx,
        campaign_id: Annotated[int, Field(description="Apple campaign id")],
        start_time: Annotated[str | None, Field(description="New start, e.g. 2026-12-01; omit to keep")] = None,
        end_time: Annotated[str | None, Field(description="New end, e.g. 2026-12-31; omit to keep")] = None,
    ) -> Proposal:
        """Show what moving a campaign's start or end would do. Writes nothing.

        Ending earlier is free. Moving the start earlier, or the end later, lets
        the campaign spend on days it would not have: projected at the whole
        daily budget and counted like an enable. Times are in the account's
        timezone.
        """
        campaign = ctx.campaign(campaign_id)
        current_start = getattr(campaign, "start_time", None)
        current_end = getattr(campaign, "end_time", None)
        new_start = _parse_when(start_time, "start_time") or current_start
        new_end = _parse_when(end_time, "end_time") or current_end
        if start_time in (None, "") and end_time in (None, ""):
            raise ToolError("give start_time, end_time or both.")
        if new_start and new_end and new_end <= new_start:
            raise ToolError("end_time must be after start_time.")
        before = schedule_text(current_start, current_end)
        after = schedule_text(new_start, new_end)
        if before == after:
            raise ToolError(f"campaign {campaign_id} already runs {after}; nothing to change.")
        widens = bool(
            (new_start and current_start and new_start < current_start)
            or (current_end and new_end and new_end > current_end)
        )
        checks = [
            check_campaign_in_scope(ctx.limits, campaign_id),
            check_known_status("campaign_status_known", enum_value(campaign.status)),
        ]
        projected = Decimal("0")
        if widens:
            projected, bound = bound_check(*campaign_enable_bound(campaign))
            checks += bound
        warnings = serving_warning(enum_value(campaign.display_status), "campaign")
        if new_end and (not current_end or new_end < current_end):
            warnings.append(f"The campaign STOPS serving at {_when_text(new_end)}.")
        path = entity_path(campaign)
        return Proposal(
            items=[make_item(
                entity_type="campaign", entity_id=campaign_id, entity_name=campaign.name,
                path=path, field="schedule", before=before, after=after,
                campaign_id=campaign_id, projected=projected,
            )],
            checks=checks, projected=projected, is_enable=widens, warnings=warnings,
            note="Ending earlier is free; starting earlier or ending later is counted like an enable.",
            entity_type="campaign", entity_id=str(campaign_id), entity_name=campaign.name,
            path=path, field_name="schedule", before=before, after=after, campaign=campaign,
            entity_serving=enum_value(campaign.display_status),
        )

    def current(self, ctx, item):
        campaign = client.get_campaign(int(item["entity_id"]))
        return schedule_text(getattr(campaign, "start_time", None), getattr(campaign, "end_time", None))

    def request(self, ctx, items):
        start, end = parse_schedule(items[0]["after"])
        kwargs: dict[str, Any] = {}
        if start:
            kwargs["start_time"] = start
        if end:
            kwargs["end_time"] = end
        return "campaigns_id_put", {
            "id": str(items[0]["entity_id"]),
            "x_ap_context": context_header(),
            "campaign_update": CampaignUpdate(**kwargs),
        }

    def observe(self, ctx, response, items):
        updated = unwrap(response, "campaign update")
        observed = (
            schedule_text(getattr(updated, "start_time", None), getattr(updated, "end_time", None))
            if updated else None
        )
        return [Outcome(observed == items[0]["after"], observed)]

    def revert_projection(self, ctx, entry, item):
        old_start, old_end = parse_schedule(str(item["before"]))
        new_start, new_end = parse_schedule(str(item["after"]))
        widens = bool((new_start and old_start and new_start < old_start)
                      or (old_end and new_end and new_end > old_end))
        if not widens:
            return Decimal("0"), False
        bound, reason = campaign_enable_bound(ctx.campaign(item.get("campaign_id")))
        if bound is None:
            raise ValueError(f"cannot revert: {reason}.")
        return bound, True

    def inverse(self, ctx, entry):
        _start, end = parse_schedule(str(entry["before"]))
        if end is None and parse_schedule(str(entry["after"]))[1] is not None:
            raise ValueError("the campaign had no end time before, and 'no end' cannot be written back.")
        return self, make_item(
            entity_type="campaign", entity_id=entry["entity_id"], entity_name=entry.get("entity_name"),
            path=str(entry.get("path")), field="schedule", before=str(entry["after"]),
            after=str(entry["before"]), campaign_id=entry.get("campaign_id"),
        )

