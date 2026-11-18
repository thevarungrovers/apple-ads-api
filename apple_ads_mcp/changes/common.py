"""Reads, warnings and building blocks several change kinds share."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from apple_ads_platform import BidStrategyGoal, BidStrategyType, BidStrategyUpdate
from mcp.server.mcpserver.exceptions import ToolError

from apple_ads_mcp import client
from apple_ads_mcp.changes.base import ChangeKind, Outcome, item as make_item
from apple_ads_mcp.client import (
    context_header,
    enum_value,
    eq_filter,
    format_money,
    landed,
    money_amount,
    money_currency,
    query,
    to_money,
    unwrap,
)
from apple_ads_mcp.previews import ChangePreview, Metrics7d

SETTABLE_STATUSES = {"ENABLED", "PAUSED"}


def parse_choice(raw: str, allowed: set[str], field: str = "status") -> str:
    value = str(raw).strip().upper()
    if value not in allowed:
        raise ToolError(f"{field}={raw!r} must be one of {', '.join(sorted(allowed))}")
    return value


# --- warnings -------------------------------------------------------------------


def serving_warning(entity_status: str | None, kind: str) -> list[str]:
    """Warn from the ENTITY's serving status, not its campaign's.

    A keyword under a paused ad group reads AD_GROUP_ON_HOLD while its campaign
    reads RUNNING. Warning off the campaign would shout about live traffic that
    this entity cannot receive -- and a warning that cries wolf is one people
    learn to scroll past.
    """
    if entity_status == "RUNNING":
        return [f"This {kind} IS SERVING TODAY -- the change takes effect on live traffic."]
    if entity_status in (None, "unknown_default_open_api"):
        return [f"Could not determine whether this {kind} is serving. Treat it as live."]
    return [
        f"This {kind} is {entity_status}, so it is not spending right now. The "
        f"change takes effect whenever it next serves."
    ]


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
    return Metrics7d(spend=str(spend), taps=taps, impressions=impressions, installs=installs, days=7)


def campaign_budget(campaign: Any) -> Any:
    """The campaign's daily budget `Money`, or None."""
    budget = getattr(campaign, "daily_budget", None) if campaign is not None else None
    return getattr(budget, "value", None) if budget is not None else None


def campaign_currency(campaign: Any) -> str | None:
    """The campaign's budget currency, for something that has no money of its own."""
    return money_currency(campaign_budget(campaign))


def child_preview(
    *,
    tool: str,
    entity_type: str,
    entity_id: Any,
    entity_name: str | None,
    path: str,
    field: str,
    before: str,
    after: str,
    campaign: Any,
    entity_serving: str | None,
    projected: Decimal,
    checks: list[str] | None = None,
    blocked: bool = False,
    warnings: list[str] | None = None,
    metrics: Metrics7d | None = None,
    pct: str | None = None,
) -> ChangePreview:
    """One item of a batch, shown under the batch token."""
    budget = campaign_budget(campaign)
    return ChangePreview(
        preview_token="(covered by the batch token)",
        tool=tool,
        entity_type=entity_type,
        entity_id=str(entity_id),
        entity_name=entity_name or "(unnamed)",
        path=path,
        field=field,
        before=before,
        after=after,
        pct_change=pct,
        campaign_id=str(campaign.id) if campaign is not None and campaign.id else None,
        campaign_name=getattr(campaign, "name", None),
        campaign_daily_budget=format_money(budget) if budget else None,
        campaign_serving_status=enum_value(getattr(campaign, "display_status", None)),
        entity_serving_status=entity_serving,
        last_7d=metrics,
        projected_daily_spend_delta=str(projected),
        guardrail_checks=checks or [],
        blocked=blocked,
        warnings=warnings or [],
    )


# --- bid strategy -----------------------------------------------------------------


def bid_strategy_type(ad_group: Any) -> str | None:
    """MANUAL_CPT / MAX_CONVERSIONS / ... off an ad group, or None if unreadable."""
    strategy = getattr(ad_group, "bid_strategy", None)
    if strategy is None:
        return None
    return enum_value(getattr(strategy, "bid_strategy_type", None))


def bid_strategy_goal(ad_group: Any) -> str | None:
    """TAP / INSTALL / ... off an ad group, or None if unreadable."""
    strategy = getattr(ad_group, "bid_strategy", None)
    if strategy is None:
        return None
    return enum_value(getattr(strategy, "bid_strategy_goal", None))


def ad_group_bid(ad_group: Any) -> Any:
    """The `Money` at `bidStrategy.bid`, or None.

    There is no `defaultBid` field on an ad group or on AdGroupUpdate; the bid
    lives inside the nested bidStrategy object. Verified against the live API
    2026-10-09, and `tools_read._ad_group_row` reads it from the same place.
    """
    strategy = getattr(ad_group, "bid_strategy", None)
    if strategy is None:
        return None
    return getattr(strategy, "bid", None)


def bid_strategy_update(
    amount: str | None,
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
    kwargs: dict[str, Any] = {}
    if amount is not None:
        kwargs["bid"] = to_money(amount, currency)
    if strategy_type:
        kwargs["bid_strategy_type"] = BidStrategyType(strategy_type)
    if strategy_goal:
        kwargs["bid_strategy_goal"] = BidStrategyGoal(strategy_goal)
    return BidStrategyUpdate(**kwargs)


def default_bid_blast_radius(ad_group_id: int) -> list[str]:
    """How many keywords this default bid actually governs.

    A keyword carrying a bid of its own ignores the ad group default, so "this
    reprices 40 keywords" would be false where 38 are explicitly bid. Degrades to
    a warning that says so rather than failing the preview: the rest of the
    preview is still worth reading if this one query fails.
    """
    try:
        response = client.call(
            "keywords_query_post",
            x_ap_context=context_header(),
            query_request=query(page_size=1000, filters=[eq_filter("adGroupId", ad_group_id)]),
        )
        rows = [k for k in (unwrap(response, "keywords") or []) if not k.deleted]
        truncated = False
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
        f"This ad group has {len(rows)}{'+' if truncated else ''} keyword(s): "
        f"{len(rows) - own} fall back to the default bid and are repriced by this "
        f"change; {own} carry their own bid and are NOT affected."
    ]


def existing_negatives(
    campaign_id: int | None, ad_group_id: int | None
) -> tuple[set[tuple[str, str]], str | None]:
    """((lowercased text, match type) already present, why it could not be checked)."""
    filters = []
    if ad_group_id is not None:
        filters.append(eq_filter("adGroupId", ad_group_id))
    elif campaign_id is not None:
        filters.append(eq_filter("campaignId", campaign_id))
    try:
        rows = unwrap(
            client.call(
                "negative_keywords_query_post",
                x_ap_context=context_header(),
                query_request=query(page_size=1000, filters=filters or None),
            ),
            "negative keywords",
        ) or []
    except ToolError:
        return set(), None
    return (
        {
            ((row.text or "").lower(), enum_value(row.match_type) or "")
            for row in rows
            if not row.deleted
        },
        None,
    )


# --- how much an enable can spend ---------------------------------------------------


def campaign_headroom(ctx, campaign: Any) -> tuple[Decimal | None, str]:
    """How much MORE the campaign could spend per day than it does now.

    The honest upper bound for anything that widens delivery INSIDE a campaign
    -- enabling a keyword, ad group or ad, pausing a negative: none of them can
    take the campaign past its daily budget, and the last 7 days say how much of
    that budget it already uses. Zero, with a reason, when the campaign is not
    enabled: then nothing under it spends until the campaign itself is enabled,
    and THAT change projects the whole budget.

    None means it cannot be bounded at all (no daily budget of its own), which
    the caller turns into a FAIL rather than a confident zero.
    """
    if campaign is None:
        return None, "the campaign could not be read, so the added spend cannot be bounded"
    status = enum_value(getattr(campaign, "status", None))
    if status != "ENABLED":
        return Decimal("0"), (
            f"the campaign is {status}, so this adds no spend until the campaign is enabled"
        )
    budget = money_amount(campaign_budget(campaign))
    if budget is None:
        return None, (
            "the campaign has no daily budget of its own (a shared budget?), so the "
            "added spend cannot be bounded"
        )
    spend, _taps, _impressions, _installs, error = ctx.metrics_7d(
        "apps_campaign_reports", "campaignId", campaign.id
    )
    if error:
        return budget, (
            f"upper bound: the whole daily budget {budget}, because the last 7 days "
            f"could not be read ({error[:120]})"
        )
    average = (spend / Decimal(7)).quantize(Decimal("0.01"))
    headroom = max(budget - average, Decimal("0")).quantize(Decimal("0.01"))
    return headroom, (
        f"upper bound: the campaign's unused daily budget ({budget} budget - "
        f"{average}/day average over the last 7 days)"
    )


def campaign_enable_bound(campaign: Any) -> tuple[Decimal | None, str]:
    """Enabling a whole campaign can spend its whole daily budget, from today."""
    budget = money_amount(campaign_budget(campaign))
    if budget is None:
        return None, (
            "the campaign has no daily budget of its own (a shared budget?), so the "
            "spend it resumes cannot be bounded"
        )
    return budget, f"upper bound: the campaign's whole daily budget ({budget})"


def bound_check(projected: Decimal | None, reason: str) -> tuple[Decimal, list]:
    """(projection to use, [FAIL check] when it could not be bounded)."""
    from apple_ads_mcp.guardrails import Check

    if projected is None:
        return Decimal("0"), [Check("spend_is_bounded", False, reason)]
    return projected, [Check("spend_is_bounded", True, reason)]


# --- a status flip, shared by every entity that has one ---------------------------


class StatusKind(ChangeKind):
    """ENABLED <-> PAUSED on one entity. Subclasses say which entity and how.

    Pausing and un-pausing are one call each, so the inverse is the opposite
    status and revert_change can always undo one.
    """

    entity_type = ""
    label = ""
    get_method = ""
    put_method = ""
    update_arg = ""

    def update_body(self, status: str) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def fetch(self, entity_id: Any) -> Any:
        response = client.call(self.get_method, id=str(entity_id), x_ap_context=context_header())
        entity = unwrap(response, self.label)
        if entity is None:
            raise ToolError(f"no {self.label} {entity_id} in this ad account")
        return entity

    def current(self, ctx, item: dict[str, Any]) -> str | None:
        return enum_value(self.fetch(item["entity_id"]).status) or "unknown"

    def request(self, ctx, items):
        item = items[0]
        return self.put_method, {
            "id": str(item["entity_id"]),
            "x_ap_context": context_header(),
            self.update_arg: self.update_body(item["after"]),
        }

    def observe(self, ctx, response, items):
        updated = unwrap(response, f"{self.label} update")
        observed = enum_value(getattr(updated, "status", None)) if updated else None
        return [Outcome(landed(observed, items[0]["after"]), observed)]

    def ledger_keys_for(self) -> tuple[tuple[str, str], ...]:
        return ((self.entity_type, "status"),)

    def enable_bound(self, ctx, campaign_id: Any) -> tuple[Decimal | None, str]:
        """What enabling one of these can add per day: the campaign's headroom."""
        return campaign_headroom(ctx, ctx.campaign(campaign_id))

    def revert_projection(self, ctx, entry, item):
        if item["after"] != "ENABLED":
            return Decimal("0"), False
        bound, reason = self.enable_bound(ctx, item.get("campaign_id"))
        if bound is None:
            raise ValueError(f"cannot revert: {reason}. Re-enable it with its preview instead.")
        return bound, True

    def inverse(self, ctx, entry):
        return self, make_item(
            entity_type=self.entity_type,
            entity_id=entry["entity_id"],
            entity_name=entry.get("entity_name"),
            path=str(entry.get("path")),
            field="status",
            before=str(entry["after"]),
            after=str(entry["before"]),
            campaign_id=entry.get("campaign_id"),
        )

    def pause_item(self, entity_id: Any, entity_name: str | None, path: str, campaign_id: Any, before: str):
        """The item that pauses one entity -- a creation's inverse."""
        return make_item(
            entity_type=self.entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            path=path,
            field="status",
            before=before,
            after="PAUSED",
            campaign_id=campaign_id,
        )
