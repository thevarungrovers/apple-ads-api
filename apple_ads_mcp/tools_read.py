"""Read-only tools. Every one of them carries `read_only_hint=True`.

A read-only server is independently useful -- it is the whole "ask the agent what
the account is doing" half of the job -- and it is the natural stopping point if
the write half slips. Nothing here mutates anything, and no tool here takes a
preview token.

`get_guardrails` is in this file rather than with the write tools on purpose: an
agent that can read its own limits before proposing a change proposes fewer
changes that will be refused.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Any

from apple_ads_platform import (
    AppsOptions,
    AppsReportingRequest,
    AuditFilter,
    AuditQuery,
    Filter,
    Pagination,
    RecommendationQueryRequest,
    TimeRange,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from apple_ads_mcp import client, ledger
from apple_ads_mcp.guardrails import writes_disabled
from apple_ads_mcp.client import (
    REPORT_TIMEOUT,
    call,
    context_header,
    enum_value,
    eq_filter,
    format_money,
    in_filter,
    money_amount,
    query,
    unwrap,
)

READ_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=True)
LOCAL_ONLY = ToolAnnotations(read_only_hint=True, idempotent_hint=True, open_world_hint=False)

MAX_REPORT_DAYS = 90  # Apple's DAILY granularity window


# --- output models ------------------------------------------------------------


class AdAccountInfo(BaseModel):
    ad_account_id: str
    org_id: str | None
    name: str | None
    roles: list[str]
    is_active_account: bool = Field(
        description="True for the account this server is configured to address"
    )


class WhoAmI(BaseModel):
    ad_account_id_in_use: str
    context_header: str
    org_id_from_me: str | None
    user_id: str | None
    accounts: list[AdAccountInfo]
    note: str


class CampaignRow(BaseModel):
    id: str
    name: str | None
    status: str | None
    display_status: str | None = Field(description="RUNNING means it can spend today")
    daily_budget: str | None
    supply_placements: list[str] | None = Field(
        default=None, description="e.g. APPSTORE_SEARCH_RESULTS, APPSTORE_SEARCH_TAB"
    )
    countries_or_regions: list[str] | None = None
    deleted: bool | None = None


class CampaignList(BaseModel):
    total: int | None
    returned: int
    campaigns: list[CampaignRow]


class AdGroupRow(BaseModel):
    id: str
    campaign_id: str | None
    name: str | None
    status: str | None
    display_status: str | None
    default_bid: str | None = Field(
        default=None, description="From bidStrategy.bid, when the ad group carries one"
    )
    automated_keywords_opt_in: bool | None = None
    deleted: bool | None = None


class AdGroupList(BaseModel):
    total: int | None
    returned: int
    ad_groups: list[AdGroupRow]


class KeywordRow(BaseModel):
    id: str
    campaign_id: str | None
    ad_group_id: str | None
    text: str | None
    match_type: str | None
    bid: str | None
    status: str | None
    display_status: str | None
    deleted: bool | None = None


class KeywordList(BaseModel):
    total: int | None
    returned: int
    keywords: list[KeywordRow]


class NegativeKeywordRow(BaseModel):
    id: str
    campaign_id: str | None
    ad_group_id: str | None
    text: str | None
    match_type: str | None
    status: str | None
    deleted: bool | None = None


class NegativeKeywordList(BaseModel):
    total: int | None
    returned: int
    negative_keywords: list[NegativeKeywordRow]


class SharedBudgetRow(BaseModel):
    id: str
    name: str | None
    amount: str | None
    status: str | None


class SharedBudgetList(BaseModel):
    returned: int
    shared_budgets: list[SharedBudgetRow]
    note: str


class ReportRow(BaseModel):
    id: str | None
    name: str | None
    extra: dict[str, str] = Field(default_factory=dict)
    spend: str
    impressions: int
    taps: int
    installs: int
    new_downloads: int
    redownloads: int
    avg_cpt: str | None = None
    avg_cpi: str | None = None


class Report(BaseModel):
    entity: str
    start: str
    end: str
    granularity: str
    rows: list[ReportRow]
    grand_total: ReportRow | None
    note: str


class SuggestionRow(BaseModel):
    text: str | None
    match_type: str | None
    bid: str | None
    extra: dict[str, str] = Field(default_factory=dict)


class SuggestionList(BaseModel):
    returned: int
    suggestions: list[SuggestionRow]


class BudgetRecommendationRow(BaseModel):
    campaign_id: str | None
    current_daily_budget: str | None
    recommended_daily_budget: str | None
    extra: dict[str, str] = Field(default_factory=dict)


class BudgetRecommendationList(BaseModel):
    returned: int
    recommendations: list[BudgetRecommendationRow]
    note: str


class AuditRow(BaseModel):
    transaction_id: str | None
    event_type: str | None
    event_time: str | None
    entity_type: str | None
    entity_ids: list[str] = Field(
        default_factory=list, description="The entities this transaction touched"
    )
    detail_ids: list[str] = Field(
        default_factory=list,
        description="Composite ids for get_change_details: EntityType.entityId.txnId",
    )
    count: int | None
    modified_by: str | None
    user_type: str | None


class AuditList(BaseModel):
    returned: int
    changes: list[AuditRow]
    note: str


class GuardrailReport(BaseModel):
    source: str
    kill_switch_active: bool
    kill_switch_reason: str | None
    limits: dict[str, str]
    allowed_campaign_ids: list[str]
    allowed_currencies: list[str]
    session: dict[str, str]
    pending_previews: int
    note: str


class LedgerEntry(BaseModel):
    entry_id: str
    ts: str | None
    tool: str | None
    entity_type: str | None
    entity_id: str | None
    entity_name: str | None
    path: str | None
    field: str | None
    before: str | None
    after: str | None
    outcome: str | None = Field(
        description="applied | failed | partial | refused, or null when the write "
        "started and nothing recorded how it ended"
    )
    outcome_detail: str | None = None
    reverts_entry_id: str | None = None


class LedgerList(BaseModel):
    returned: int
    entries: list[LedgerEntry]
    unfinished: int = Field(
        description="Entries with an intent and no outcome -- a write that may have "
        "landed at Apple with nothing local to say so"
    )
    note: str


class ReconcileReport(BaseModel):
    since: str
    local_entries: int
    apple_changes: int
    local_without_apple: list[LedgerEntry]
    apple_without_local: list[AuditRow]
    note: str


# --- helpers ------------------------------------------------------------------


def _window(days: int) -> tuple[dt.date, dt.date]:
    days = max(1, min(days, MAX_REPORT_DAYS))
    end = dt.date.today()
    return end - dt.timedelta(days=days - 1), end


def _metrics_row(metrics: Any, *, id_: Any = None, name: Any = None, extra: dict | None = None) -> ReportRow:
    return ReportRow(
        id=str(id_) if id_ is not None else None,
        name=name,
        extra={k: str(v) for k, v in (extra or {}).items() if v is not None},
        spend=format_money(getattr(metrics, "local_spend", None)),
        impressions=getattr(metrics, "impressions", 0) or 0,
        taps=getattr(metrics, "taps", 0) or 0,
        installs=getattr(metrics, "total_installs", 0) or 0,
        new_downloads=getattr(metrics, "total_new_downloads", 0) or 0,
        redownloads=getattr(metrics, "total_redownloads", 0) or 0,
        avg_cpt=format_money(getattr(metrics, "cpt", None)),
        avg_cpi=format_money(getattr(metrics, "total_avg_cpi", None)),
    )


def run_report(
    method: str,
    entity: str,
    days: int,
    filters: list[Filter] | None = None,
    granularity: str = "DAILY",
    group_by: list[str] | None = None,
) -> tuple[list[Any], Any, dt.date, dt.date]:
    """One `apps_*_reports` call. Returns (rows, grand_total, start, end).

    `_request_timeout` is lifted to 60s here: a report is the one read in this API
    that routinely outruns the client's 30s default.
    """
    start, end = _window(days)
    response = call(
        method,
        x_ap_context=context_header(),
        apps_reporting_request=AppsReportingRequest(
            time_range=TimeRange(start=start, end=end, granularity=granularity),
            filters=filters or None,
            group_by=group_by or None,
            options=AppsOptions(include_rows=["GRAND_TOTAL"]),
        ),
        _request_timeout=REPORT_TIMEOUT,
    )
    result = unwrap(response, f"{entity} report")
    rows = (getattr(result, "rows", None) or []) if result else []
    summary = getattr(result, "summary", None) if result else None
    return rows, getattr(summary, "grand_total", None), start, end


def report_filter(field: str, values: list[Any]) -> Filter:
    return Filter(field=field, operator="IN", value=list(values))


def entity_metrics_7d(method: str, field: str, entity_id: int) -> tuple[Decimal, int, int, int]:
    """(spend, taps, impressions, installs) over 7 days for one entity.

    Failure here is NOT fatal to a preview. A preview without 7-day numbers is
    still worth showing -- it just loses the projection -- whereas a preview that
    refuses to render because reporting hiccuped teaches you to skip previews.
    """
    try:
        _rows, grand_total, _start, _end = run_report(
            method, field, days=7, filters=[report_filter(field, [entity_id])]
        )
    except ToolError:
        return Decimal("0"), 0, 0, 0
    if grand_total is None:
        return Decimal("0"), 0, 0, 0
    spend = money_amount(getattr(grand_total, "local_spend", None)) or Decimal("0")
    return (
        spend,
        getattr(grand_total, "taps", 0) or 0,
        getattr(grand_total, "impressions", 0) or 0,
        getattr(grand_total, "total_installs", 0) or 0,
    )


def _included(targeting_data: Any) -> list[str] | None:
    """The `include` list out of a Platform API targeting block."""
    if targeting_data is None:
        return None
    values = getattr(targeting_data, "include", None)
    return [enum_value(v) for v in values] if values else None


def _campaign_row(campaign: Any) -> CampaignRow:
    budget = campaign.daily_budget.value if getattr(campaign, "daily_budget", None) else None
    # Platform API targeting is {field: {include: [...], exclude: [...]}}, and the
    # field names are SINGULAR (supplyPlacement, countryOrRegion) where v5 used
    # plurals. Reading the v5 names here returns None silently, which looks like
    # "this campaign targets nothing" rather than like a bug.
    targeting = getattr(campaign, "targeting", None)
    return CampaignRow(
        id=str(campaign.id),
        name=campaign.name,
        status=enum_value(campaign.status),
        display_status=enum_value(campaign.display_status),
        daily_budget=format_money(budget) if budget else None,
        supply_placements=_included(getattr(targeting, "supply_placement", None)),
        countries_or_regions=_included(getattr(targeting, "country_or_region", None)),
        deleted=campaign.deleted,
    )


def _ad_group_row(ad_group: Any) -> AdGroupRow:
    bid_strategy = getattr(ad_group, "bid_strategy", None)
    bid = getattr(bid_strategy, "bid", None) if bid_strategy else None
    return AdGroupRow(
        id=str(ad_group.id),
        campaign_id=str(ad_group.campaign_id) if ad_group.campaign_id else None,
        name=ad_group.name,
        status=enum_value(ad_group.status),
        display_status=enum_value(ad_group.display_status),
        default_bid=format_money(bid) if bid else None,
        automated_keywords_opt_in=ad_group.automated_keywords_opt_in,
        deleted=ad_group.deleted,
    )


def _keyword_row(keyword: Any) -> KeywordRow:
    return KeywordRow(
        id=str(keyword.id),
        campaign_id=str(keyword.campaign_id) if keyword.campaign_id else None,
        ad_group_id=str(keyword.ad_group_id) if keyword.ad_group_id else None,
        text=keyword.text,
        match_type=enum_value(keyword.match_type),
        bid=format_money(keyword.bid) if keyword.bid else None,
        status=enum_value(keyword.status),
        display_status=enum_value(keyword.display_status),
        deleted=keyword.deleted,
    )


def _negative_row(negative: Any) -> NegativeKeywordRow:
    return NegativeKeywordRow(
        id=str(negative.id),
        campaign_id=str(negative.campaign_id) if negative.campaign_id else None,
        ad_group_id=str(negative.ad_group_id) if negative.ad_group_id else None,
        text=negative.text,
        match_type=enum_value(negative.match_type),
        status=enum_value(negative.status),
        deleted=negative.deleted,
    )


def _ledger_entry(entry: dict[str, Any]) -> LedgerEntry:
    def text(key: str) -> str | None:
        value = entry.get(key)
        return None if value is None else str(value)

    return LedgerEntry(
        entry_id=str(entry.get("entry_id")),
        ts=text("ts"),
        tool=text("tool"),
        entity_type=text("entity_type"),
        entity_id=text("entity_id"),
        entity_name=text("entity_name"),
        path=text("path"),
        field=text("field"),
        before=text("before"),
        after=text("after"),
        outcome=text("outcome"),
        outcome_detail=text("outcome_detail"),
        reverts_entry_id=text("reverts_entry_id"),
    )


def _audit_row(summary: Any) -> AuditRow:
    # `metas` is where the entity ids are: a list of dicts keyed by the entity
    # type ({"Campaign": "2144...", "detailId": "Campaign.2144....<txn>"}). The
    # summary itself is grouped by TRANSACTION, so without metas there is no way
    # to say which entity a row is about -- and detailId is the composite key
    # get_change_details needs, which is why it cannot be constructed by hand.
    # A meta also carries a nested `metadata` dict (the entity's fields at the
    # time of the change). Taking every value would stringify that whole dict
    # into the id list, so only scalars count as ids.
    entity_ids, detail_ids = [], []
    for meta in summary.metas or []:
        for key, value in (meta or {}).items():
            if value is None or isinstance(value, (dict, list)):
                continue
            if key == "detailId":
                detail_ids.append(str(value))
            else:
                entity_ids.append(str(value))
    return AuditRow(
        transaction_id=summary.transaction_id,
        event_type=enum_value(summary.event_type),
        event_time=summary.event_time.isoformat() if summary.event_time else None,
        entity_type=summary.entity_type,
        entity_ids=entity_ids,
        detail_ids=detail_ids,
        count=summary.count,
        modified_by=summary.modified_by,
        user_type=enum_value(summary.user_type),
    )


# Apple requires an entityType on every change-history query and returns one
# type per call, so "what changed recently" is four calls, not one. Verified
# against the live API 2026-10-08: anything else is a bare HTTP 400 with no
# detail, or `VALIDATION_ERROR: EntityType required`.
AUDIT_ENTITY_TYPES = ("Campaign", "AdGroup", "Keyword", "NegativeKeyword")


def query_audit(
    since: dt.datetime,
    limit: int = 200,
    until: dt.datetime | None = None,
    entity_types: tuple[str, ...] = AUDIT_ENTITY_TYPES,
) -> list[Any]:
    """`query_audit_summary` over a time window, across every entity type.

    eventTime takes BETWEEN with a start and an end. GREATER_THAN_OR_EQUAL_TO is
    documented as supported for time fields but answers a bare 400 in practice,
    so a range is what this sends.
    """
    until = until or dt.datetime.now(dt.timezone.utc)
    rows: list[Any] = []
    for entity_type in entity_types:
        response = call(
            "query_audit_summary",
            x_ap_context=context_header(),
            audit_query=AuditQuery(
                filters=[
                    AuditFilter(
                        field="eventTime",
                        operator="BETWEEN",
                        value=[
                            since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            until.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        ],
                    ),
                    AuditFilter(field="entityType", operator="EQUALS", value=entity_type),
                ],
                pagination=Pagination(offset=0, page_size=min(max(1, limit), 1000)),
            ),
        )
        rows.extend(unwrap(response, f"{entity_type} audit summary") or [])
    rows.sort(key=lambda r: r.event_time or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    return rows


# --- registration -------------------------------------------------------------


def register(mcp, state) -> None:  # noqa: C901 -- a flat list of tool definitions
    @mcp.tool(
        title="Who am I, and which Apple Ads account is this server writing to?",
        annotations=READ_ONLY,
    )
    def whoami() -> WhoAmI:
        """Identify the credential and the ad account every other tool addresses.

        The only call in this API that sends no context header, so it works even
        when the configured adAccountId is wrong -- which makes it the right
        first thing to run when anything else returns 401/403.
        """
        me = unwrap(call("get_me"), "me")
        acls = unwrap(call("get_user_acls"), "ACLs")
        in_use = client.ad_account()
        accounts = [
            AdAccountInfo(
                ad_account_id=str(getattr(acl.ad_account, "id", "")),
                org_id=str(getattr(acl.ad_account, "org_id", "")) or None,
                name=getattr(acl.ad_account, "name", None),
                roles=list(acl.roles or []),
                is_active_account=str(getattr(acl.ad_account, "id", "")) == in_use,
            )
            for acl in (getattr(acls, "acls", None) or [])
        ]
        return WhoAmI(
            ad_account_id_in_use=in_use,
            context_header=context_header(),
            org_id_from_me=str(me.org_id) if me and me.org_id else None,
            user_id=str(me.user_id) if me and me.user_id else None,
            accounts=accounts,
            note=(
                "adAccountId is NOT the org id -- one org can own several ad "
                "accounts. Every other tool addresses ad_account_id_in_use."
            ),
        )

    @mcp.tool(title="List campaigns in this ad account", annotations=READ_ONLY)
    def list_campaigns(
        include_deleted: Annotated[
            bool, Field(description="Include campaigns Apple has marked deleted")
        ] = False,
        limit: Annotated[int, Field(description="Max campaigns to return", ge=1, le=1000)] = 100,
    ) -> CampaignList:
        """Every campaign, with status, serving status and daily budget."""
        response = call(
            "campaigns_query_post",
            x_ap_context=context_header(),
            query_request=query(page_size=limit),
        )
        rows = unwrap(response, "campaigns") or []
        if not include_deleted:
            rows = [c for c in rows if not c.deleted]
        total = getattr(getattr(response, "pagination", None), "total_count", None)
        return CampaignList(
            total=total, returned=len(rows), campaigns=[_campaign_row(c) for c in rows]
        )

    @mcp.tool(title="Get one campaign by id", annotations=READ_ONLY)
    def get_campaign(campaign_id: Annotated[int, Field(description="Apple campaign id")]) -> CampaignRow:
        """Full detail for a single campaign."""
        return _campaign_row(client.get_campaign(campaign_id))

    @mcp.tool(title="List ad groups", annotations=READ_ONLY)
    def list_ad_groups(
        campaign_id: Annotated[
            int | None, Field(description="Limit to one campaign; omit for all")
        ] = None,
        include_deleted: Annotated[bool, Field(description="Include deleted ad groups")] = False,
        limit: Annotated[int, Field(description="Max ad groups to return", ge=1, le=1000)] = 200,
    ) -> AdGroupList:
        """Ad groups, with status, serving status and default bid."""
        filters = [eq_filter("campaignId", campaign_id)] if campaign_id else None
        response = call(
            "adgroups_query_post",
            x_ap_context=context_header(),
            query_request=query(page_size=limit, filters=filters),
        )
        rows = unwrap(response, "ad groups") or []
        if not include_deleted:
            rows = [g for g in rows if not g.deleted]
        total = getattr(getattr(response, "pagination", None), "total_count", None)
        return AdGroupList(
            total=total, returned=len(rows), ad_groups=[_ad_group_row(g) for g in rows]
        )

    @mcp.tool(title="Get one ad group by id", annotations=READ_ONLY)
    def get_ad_group(ad_group_id: Annotated[int, Field(description="Apple ad group id")]) -> AdGroupRow:
        """Full detail for a single ad group."""
        return _ad_group_row(client.get_ad_group(ad_group_id))

    @mcp.tool(title="List targeting keywords", annotations=READ_ONLY)
    def list_keywords(
        ad_group_id: Annotated[int | None, Field(description="Limit to one ad group")] = None,
        campaign_id: Annotated[int | None, Field(description="Limit to one campaign")] = None,
        include_deleted: Annotated[bool, Field(description="Include deleted keywords")] = False,
        limit: Annotated[int, Field(description="Max keywords to return", ge=1, le=1000)] = 200,
    ) -> KeywordList:
        """Keywords with their bids, match types and serving status.

        A Search Match ad group returns NO keywords. That is correct data, not a
        lookup failure -- Apple matches the search term against the App Store
        listing and reports no keywordId at all.
        """
        filters = []
        if ad_group_id:
            filters.append(eq_filter("adGroupId", ad_group_id))
        if campaign_id:
            filters.append(eq_filter("campaignId", campaign_id))
        response = call(
            "keywords_query_post",
            x_ap_context=context_header(),
            query_request=query(page_size=limit, filters=filters or None),
        )
        rows = unwrap(response, "keywords") or []
        if not include_deleted:
            rows = [k for k in rows if not k.deleted]
        total = getattr(getattr(response, "pagination", None), "total_count", None)
        return KeywordList(total=total, returned=len(rows), keywords=[_keyword_row(k) for k in rows])

    @mcp.tool(title="Get one keyword by id", annotations=READ_ONLY)
    def get_keyword(keyword_id: Annotated[int, Field(description="Apple keyword id")]) -> KeywordRow:
        """Full detail for a single keyword, including its current bid."""
        return _keyword_row(client.get_keyword(keyword_id))

    @mcp.tool(title="List negative keywords", annotations=READ_ONLY)
    def list_negative_keywords(
        ad_group_id: Annotated[int | None, Field(description="The ad group to list")] = None,
        campaign_id: Annotated[
            int | None,
            Field(description="List every ad group in this campaign, one query each"),
        ] = None,
        include_deleted: Annotated[bool, Field(description="Include deleted negatives")] = False,
        limit: Annotated[int, Field(description="Max to return", ge=1, le=1000)] = 200,
    ) -> NegativeKeywordList:
        """Negative keywords for an ad group, or for every ad group in a campaign.

        Apple REQUIRES an adGroupId condition on this query -- a campaignId
        filter alone is rejected with
        `VALIDATION_ERROR: adGroupId condition is required`. So passing
        campaign_id here fans out over that campaign's ad groups and merges the
        results; it is several calls, not one.

        A consequence worth knowing: negatives attached at CAMPAIGN level do not
        belong to any ad group, so this endpoint cannot return them. Check the
        Apple Ads UI if you need to be sure a campaign-level negative exists.
        """
        if ad_group_id is None and campaign_id is None:
            raise ToolError(
                "give ad_group_id, or campaign_id to sweep every ad group in a "
                "campaign. Apple rejects this query without an adGroupId condition."
            )

        if ad_group_id is not None:
            ad_group_ids = [ad_group_id]
        else:
            groups = unwrap(
                call(
                    "adgroups_query_post",
                    x_ap_context=context_header(),
                    query_request=query(page_size=1000, filters=[eq_filter("campaignId", campaign_id)]),
                ),
                "ad groups",
            ) or []
            ad_group_ids = [g.id for g in groups if not g.deleted]

        rows: list[Any] = []
        for group_id in ad_group_ids:
            if len(rows) >= limit:
                break
            response = call(
                "negative_keywords_query_post",
                x_ap_context=context_header(),
                query_request=query(
                    page_size=limit, filters=[eq_filter("adGroupId", group_id)]
                ),
            )
            rows.extend(unwrap(response, "negative keywords") or [])

        if not include_deleted:
            rows = [n for n in rows if not n.deleted]
        rows = rows[:limit]
        return NegativeKeywordList(
            total=len(rows),
            returned=len(rows),
            negative_keywords=[_negative_row(n) for n in rows],
        )

    @mcp.tool(title="List shared budgets", annotations=READ_ONLY)
    def list_shared_budgets(
        limit: Annotated[int, Field(description="Max to return", ge=1, le=1000)] = 100,
    ) -> SharedBudgetList:
        """Shared budgets in this account. Read only -- there is no write tool.

        `shared_budgets_id_put` in the SDK accepts no `x_ap_context` argument at
        all, so there is no way to say which account an update applies to. Until
        that is resolved, editing a shared budget is a job for the Apple Ads UI.
        """
        response = call(
            "shared_budgets_query_post",
            x_ap_context=context_header(),
            query_request=query(page_size=limit),
        )
        rows = unwrap(response, "shared budgets") or []
        out = []
        for budget in rows:
            amount = getattr(budget, "amount", None) or getattr(budget, "budget", None)
            out.append(
                SharedBudgetRow(
                    id=str(getattr(budget, "id", "")),
                    name=getattr(budget, "name", None),
                    amount=format_money(amount) if amount else None,
                    status=enum_value(getattr(budget, "status", None)),
                )
            )
        return SharedBudgetList(
            returned=len(out),
            shared_budgets=out,
            note=(
                "Read only. shared_budgets_id_put takes no account context in this "
                "SDK version, so no write tool is exposed for it."
            ),
        )

    # --- reports --------------------------------------------------------------

    @mcp.tool(title="Campaign performance report", annotations=READ_ONLY)
    def campaign_report(
        days: Annotated[int, Field(description="Window ending today", ge=1, le=90)] = 7,
        campaign_id: Annotated[int | None, Field(description="Limit to one campaign")] = None,
    ) -> Report:
        """Spend, impressions, taps and installs per campaign.

        Field names differ from the v5 API: installs are totalInstalls /
        totalNewDownloads / totalRedownloads, and the bare `installs` key v3 used
        does not exist.
        """
        filters = [report_filter("campaignId", [campaign_id])] if campaign_id else None
        rows, grand_total, start, end = run_report(
            "apps_campaign_reports", "campaign", days, filters
        )
        return Report(
            entity="campaign",
            start=start.isoformat(),
            end=end.isoformat(),
            granularity="DAILY",
            rows=[
                _metrics_row(
                    row.total_metrics,
                    id_=getattr(row.metadata, "id", None),
                    name=getattr(row.metadata, "name", None),
                )
                for row in rows
            ],
            grand_total=_metrics_row(grand_total, name="GRAND TOTAL") if grand_total else None,
            note="Totals are for the whole window, not per day.",
        )

    @mcp.tool(title="Ad group performance report", annotations=READ_ONLY)
    def ad_group_report(
        days: Annotated[int, Field(description="Window ending today", ge=1, le=90)] = 7,
        campaign_id: Annotated[int | None, Field(description="Limit to one campaign")] = None,
        ad_group_id: Annotated[int | None, Field(description="Limit to one ad group")] = None,
    ) -> Report:
        """Spend, impressions, taps and installs per ad group."""
        filters = []
        if campaign_id:
            filters.append(report_filter("campaignId", [campaign_id]))
        if ad_group_id:
            filters.append(report_filter("adGroupId", [ad_group_id]))
        rows, grand_total, start, end = run_report(
            "apps_ad_group_reports", "ad group", days, filters or None
        )
        return Report(
            entity="ad_group",
            start=start.isoformat(),
            end=end.isoformat(),
            granularity="DAILY",
            rows=[
                _metrics_row(
                    row.total_metrics,
                    id_=getattr(row.metadata, "id", None),
                    name=getattr(row.metadata, "name", None),
                    extra={"campaign_id": getattr(row.metadata, "campaign_id", None)},
                )
                for row in rows
            ],
            grand_total=_metrics_row(grand_total, name="GRAND TOTAL") if grand_total else None,
            note="Totals are for the whole window, not per day.",
        )

    @mcp.tool(title="Keyword performance report", annotations=READ_ONLY)
    def keyword_report(
        days: Annotated[int, Field(description="Window ending today", ge=1, le=90)] = 7,
        campaign_id: Annotated[int | None, Field(description="Limit to one campaign")] = None,
        ad_group_id: Annotated[int | None, Field(description="Limit to one ad group")] = None,
        keyword_id: Annotated[int | None, Field(description="Limit to one keyword")] = None,
    ) -> Report:
        """Spend and conversions per keyword -- the input to any bid decision."""
        filters = []
        if campaign_id:
            filters.append(report_filter("campaignId", [campaign_id]))
        if ad_group_id:
            filters.append(report_filter("adGroupId", [ad_group_id]))
        if keyword_id:
            filters.append(report_filter("keywordId", [keyword_id]))
        rows, grand_total, start, end = run_report(
            "apps_keyword_reports", "keyword", days, filters or None
        )
        return Report(
            entity="keyword",
            start=start.isoformat(),
            end=end.isoformat(),
            granularity="DAILY",
            rows=[
                _metrics_row(
                    row.total_metrics,
                    id_=getattr(row.metadata, "id", None),
                    name=getattr(row.metadata, "keyword", None),
                    extra={
                        "match_type": enum_value(getattr(row.metadata, "match_type", None)),
                        "ad_group_id": getattr(row.metadata, "ad_group_id", None),
                        "bid": format_money(getattr(row.metadata, "bid_amount", None)),
                    },
                )
                for row in rows
            ],
            grand_total=_metrics_row(grand_total, name="GRAND TOTAL") if grand_total else None,
            note="Totals are for the whole window, not per day.",
        )

    @mcp.tool(title="Search term report", annotations=READ_ONLY)
    def search_term_report(
        days: Annotated[int, Field(description="Window ending today", ge=1, le=90)] = 7,
        campaign_id: Annotated[int | None, Field(description="Limit to one campaign")] = None,
        ad_group_id: Annotated[int | None, Field(description="Limit to one ad group")] = None,
    ) -> Report:
        """What people actually typed -- where negative keywords come from.

        A row with no keyword id was served by Search Match, not by one of your
        keywords.
        """
        filters = []
        if campaign_id:
            filters.append(report_filter("campaignId", [campaign_id]))
        if ad_group_id:
            filters.append(report_filter("adGroupId", [ad_group_id]))
        rows, grand_total, start, end = run_report(
            "apps_search_term_reports", "search term", days, filters or None
        )
        return Report(
            entity="search_term",
            start=start.isoformat(),
            end=end.isoformat(),
            granularity="DAILY",
            rows=[
                _metrics_row(
                    row.total_metrics,
                    id_=getattr(row.metadata, "keyword_id", None),
                    name=getattr(row.metadata, "search_term_text", None),
                    extra={
                        "matched_keyword": getattr(row.metadata, "keyword", None),
                        "match_type": enum_value(getattr(row.metadata, "match_type", None)),
                        "ad_group_id": getattr(row.metadata, "ad_group_id", None),
                    },
                )
                for row in rows
            ],
            grand_total=_metrics_row(grand_total, name="GRAND TOTAL") if grand_total else None,
            note=(
                "A row with no keyword id was served by Search Match, not by a "
                "keyword you set."
            ),
        )

    # --- recommendations ------------------------------------------------------

    @mcp.tool(title="Apple's keyword suggestions", annotations=READ_ONLY)
    def keyword_suggestions(
        limit: Annotated[int, Field(description="Max suggestions", ge=1, le=200)] = 50,
    ) -> SuggestionList:
        """Keywords Apple suggests for this account. Suggestions only -- nothing is added."""
        response = call(
            "query_keyword_suggestions",
            x_ap_context=context_header(),
            recommendation_query_request=RecommendationQueryRequest(),
        )
        rows = (unwrap(response, "keyword suggestions") or [])[:limit]
        return SuggestionList(
            returned=len(rows),
            suggestions=[
                SuggestionRow(
                    text=getattr(row, "text", None) or getattr(row, "keyword", None),
                    match_type=enum_value(getattr(row, "match_type", None)),
                    bid=format_money(getattr(row, "bid", None)),
                    extra={
                        k: str(v)
                        for k, v in (row.model_dump(exclude_none=True) or {}).items()
                        if k not in {"text", "keyword", "matchType", "bid", "additional_properties"}
                    },
                )
                for row in rows
            ],
        )

    @mcp.tool(title="Apple's daily budget recommendations", annotations=READ_ONLY)
    def budget_recommendations(
        limit: Annotated[int, Field(description="Max recommendations", ge=1, le=200)] = 50,
    ) -> BudgetRecommendationList:
        """What Apple thinks each campaign's daily budget should be. READ ONLY.

        `apply_daily_budget_recommendations` exists in the API and is deliberately
        not exposed: a one-call "apply Apple's advice" would move real money with
        no preview, no bound and no ledger line.
        """
        response = call(
            "query_daily_budget_recommendations",
            x_ap_context=context_header(),
            recommendation_query_request=RecommendationQueryRequest(),
        )
        rows = (unwrap(response, "budget recommendations") or [])[:limit]
        out = []
        for row in rows:
            data = row.model_dump(by_alias=True, exclude_none=True)
            data.pop("additional_properties", None)
            out.append(
                BudgetRecommendationRow(
                    campaign_id=str(data.get("campaignId")) if data.get("campaignId") else None,
                    current_daily_budget=format_money(getattr(row, "current_daily_budget", None)),
                    recommended_daily_budget=format_money(
                        getattr(row, "recommended_daily_budget", None)
                    ),
                    extra={k: str(v) for k, v in data.items()},
                )
            )
        return BudgetRecommendationList(
            returned=len(out),
            recommendations=out,
            note=(
                "Suggestions only. To act on one, run preview_campaign_daily_budget "
                "and have a human approve it."
            ),
        )

    # --- change history -------------------------------------------------------

    @mcp.tool(title="Recent changes Apple recorded on this account", annotations=READ_ONLY)
    def list_recent_changes(
        days: Annotated[int, Field(description="How far back to look", ge=1, le=90)] = 7,
        limit: Annotated[int, Field(description="Max changes", ge=1, le=1000)] = 100,
    ) -> AuditList:
        """Apple's own audit trail -- every change, by anyone, not just this server."""
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        rows = query_audit(since, limit)[:limit]
        return AuditList(
            returned=len(rows),
            changes=[_audit_row(row) for row in rows],
            note=(
                "This is Apple's record of ALL changes, including ones made in the "
                "Apple Ads UI by a person. Compare it with list_my_changes using "
                "reconcile_ledger."
            ),
        )

    # --- local, no API call ---------------------------------------------------

    @mcp.tool(title="What am I allowed to change, and how much is left?", annotations=LOCAL_ONLY)
    def get_guardrails() -> GuardrailReport:
        """The bounds every apply_* is checked against, and this session's budget.

        Worth reading BEFORE proposing a change: a proposal inside these limits
        is one a human can approve, and one outside them will simply be refused.
        """
        limits = state.limits
        reason = writes_disabled()
        return GuardrailReport(
            source=limits.source,
            kill_switch_active=reason is not None,
            kill_switch_reason=reason,
            limits={
                "max_bid": str(limits.max_bid),
                "max_bid_increase_pct": str(limits.max_bid_increase_pct),
                "max_daily_budget": str(limits.max_daily_budget),
                "max_budget_change_pct": str(limits.max_budget_change_pct),
                "max_entities_per_call": str(limits.max_entities_per_call),
                "max_projected_delta_per_day": str(limits.max_projected_delta_per_day),
            },
            allowed_campaign_ids=sorted(str(c) for c in limits.allowed_campaign_ids),
            allowed_currencies=sorted(limits.allowed_currencies),
            session={k: str(v) for k, v in state.counters.snapshot().items()},
            pending_previews=state.previews.pending_count(),
            note=(
                "An empty allowed_campaign_ids means every campaign is in scope. "
                "Session counters reset only when the server restarts."
            ),
        )

    @mcp.tool(title="Changes this server has made (the local ledger)", annotations=LOCAL_ONLY)
    def list_my_changes(
        days: Annotated[int, Field(description="How far back to look", ge=1, le=365)] = 7,
        limit: Annotated[int, Field(description="Max entries", ge=1, le=500)] = 100,
    ) -> LedgerList:
        """Every write this server attempted, from .audit/changes.jsonl.

        An entry whose `outcome` is null is the one to look at: the write was
        started and nothing recorded how it ended, so it may have landed at
        Apple's end. `reconcile_ledger` is how you find out.
        """
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        rows = ledger.entries(since=since)[:limit]
        unfinished = sum(1 for row in rows if row.get("outcome") is None)
        return LedgerList(
            returned=len(rows),
            entries=[_ledger_entry(row) for row in rows],
            unfinished=unfinished,
            note=(
                "entry_id is what revert_change takes. A null outcome means the "
                "write was started and never recorded as finished."
            ),
        )

    @mcp.tool(
        title="Reconcile the local ledger against Apple's audit trail",
        annotations=READ_ONLY,
    )
    def reconcile_ledger(
        days: Annotated[int, Field(description="Window to reconcile", ge=1, le=90)] = 7,
    ) -> ReconcileReport:
        """Find writes that silently failed, and changes this server did not make.

        Two classes come back, and the second is the valuable one:

          local_without_apple -- we recorded a write Apple has no record of
          apple_without_local -- Apple recorded a change we did NOT make, which
                                 means somebody or something else moved this
                                 account

        Run on demand rather than per call: Apple's change DETAIL endpoint needs a
        composite id you cannot construct without querying the summary first, so
        making it a per-write dependency would double the cost of every write.
        """
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
        local = [e for e in ledger.entries(since=since) if e.get("outcome") != ledger.REFUSED]
        apple = query_audit(since, limit=1000)
        apple_rows = [_audit_row(row) for row in apple]

        # Join on entity id, which Apple exposes in each summary's `metas`. The
        # summaries are grouped by transaction, so without metas there would be
        # nothing to join on but timestamps -- and a timestamp join would call
        # every change "unexplained" the moment two happened in the same minute.
        apple_ids: set[str] = set()
        for row in apple_rows:
            apple_ids.update(row.entity_ids)

        local_ids: set[str] = set()
        for entry in local:
            for part in str(entry.get("entity_id", "")).split(","):
                part = part.strip()
                if part and part != "(new)":
                    local_ids.add(part)

        local_without_apple = [
            entry
            for entry in local
            if entry.get("outcome") in (ledger.FAILED, None)
            or not {
                part.strip()
                for part in str(entry.get("entity_id", "")).split(",")
                if part.strip() and part.strip() != "(new)"
            }
            & apple_ids
        ]
        apple_without_local = [
            row for row in apple_rows if not (set(row.entity_ids) & local_ids)
        ]

        return ReconcileReport(
            since=since.isoformat(timespec="seconds"),
            local_entries=len(local),
            apple_changes=len(apple_rows),
            local_without_apple=[_ledger_entry(e) for e in local_without_apple[:50]],
            apple_without_local=apple_without_local[:50],
            note=(
                "apple_without_local is the line that matters: it means something "
                "other than this server changed the account -- most often a person "
                "in the Apple Ads UI (userType CUSTOMER rather than CUSTOMER_API). "
                "The join is on entity id, so a change to an entity this server "
                "also touched in the window will not show up here even if it was "
                "somebody else's."
            ),
        )
