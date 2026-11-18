"""Read-only tools. Every one of them declares all four tool hints.

A read-only server is independently useful -- it is the whole "ask the agent what
the account is doing" half of the job -- and it is the natural stopping point if
the write half slips. Nothing here mutates anything, and no tool here takes a
preview token.

`get_guardrails` is in this file rather than with the write tools on purpose: an
agent that can read its own limits before proposing a change proposes fewer
changes that will be refused.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from apple_ads_platform import (
    AppsOptions,
    AppsReportingRequest,
    AuditFilter,
    AuditQuery,
    Filter,
    Pagination,
    RecommendationQueryRequest,
    RequestPagination,
    Sorting,
    TimeRange,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from apple_ads_mcp import client, ledger
from apple_ads_mcp.guardrails import DailyUsage, read_only_mode, writes_disabled
from apple_ads_mcp.client import (
    REPORT_TIMEOUT,
    context_header,
    enum_value,
    eq_filter,
    format_money,
    in_filter,
    money_amount,
    query,
    unwrap,
)

# ALL FOUR HINTS, ALWAYS, ON EVERY TOOL. A hint left unset is published as
# absent, and a client that cannot see one falls back on the spec's default --
# which for `destructiveHint` is TRUE. So an omitted hint does not mean "unknown",
# it means "assume the worst", and a plain `list_campaigns` ends up rendered with
# the same warning as a budget change. Some tool directories reject the tool
# outright. Spell every one of them out, even where the value looks obvious.
#
# READ_ONLY   reads Apple's API: no mutation, same answer twice, external service.
# LOCAL_ONLY  answers from this process and the local ledger; touches no network,
#             which is the only difference -- hence open_world_hint=False.
READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)
LOCAL_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

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
    total: int | None = Field(description="Apple's count, deleted rows included")
    returned: int
    campaigns: list[CampaignRow]
    next_offset: int | None = Field(
        default=None, description="Pass as offset for the next page; null on the last"
    )
    truncated: bool = Field(default=False, description="True when more rows exist")


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
    total: int | None = Field(description="Apple's count, deleted rows included")
    returned: int
    ad_groups: list[AdGroupRow]
    next_offset: int | None = Field(
        default=None, description="Pass as offset for the next page; null on the last"
    )
    truncated: bool = Field(default=False, description="True when more rows exist")


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
    total: int | None = Field(description="Apple's count, deleted rows included")
    returned: int
    keywords: list[KeywordRow]
    next_offset: int | None = Field(
        default=None, description="Pass as offset for the next page; null on the last"
    )
    truncated: bool = Field(default=False, description="True when more rows exist")


class NegativeKeywordRow(BaseModel):
    id: str
    campaign_id: str | None
    ad_group_id: str | None
    text: str | None
    match_type: str | None
    status: str | None
    deleted: bool | None = None


class NegativeKeywordList(BaseModel):
    total: int | None = Field(description="Apple's count, deleted rows included")
    returned: int
    negative_keywords: list[NegativeKeywordRow]
    next_offset: int | None = Field(
        default=None, description="Pass as offset for the next page; null on the last"
    )
    truncated: bool = Field(default=False, description="True when more rows exist")


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
    returned: int = 0
    offset: int = 0
    next_offset: int | None = Field(
        default=None, description="Pass as offset for the next page; null when this is the last"
    )
    total_rows_available: int | None = Field(
        default=None, description="Rows Apple has for this request across every page"
    )
    truncated: bool = Field(
        default=False, description="True when more rows exist than were returned"
    )
    time_zone: str | None = Field(
        default=None,
        description="The ad account's timezone, which start/end are dates in. Null "
        "when the account could not be read and the machine's date was used",
    )
    note: str


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
    truncated: bool = Field(
        default=False,
        description="True when at least one entity type filled its page, so older "
        "changes in the window may be missing",
    )
    note: str


class GuardrailReport(BaseModel):
    read_only_server: bool = Field(
        default=False,
        description="True when this server published no apply_* tools at all",
    )
    source: str
    kill_switch_active: bool
    kill_switch_reason: str | None
    limits: dict[str, str]
    allowed_campaign_ids: list[str]
    allowed_currencies: list[str]
    session: dict[str, str]
    daily: dict[str, str] = Field(
        default_factory=dict,
        description="Today's applies and projected spend, across every server "
        "process on this machine; resets at local midnight",
    )
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
    apple_truncated: list[str] = Field(
        default_factory=list,
        description="Entity types whose audit page came back full -- some of "
        "Apple's changes are missing, so apple_without_local is incomplete",
    )
    local_without_apple: list[LedgerEntry]
    apple_without_local: list[AuditRow]
    note: str


# --- helpers ------------------------------------------------------------------


def _window(days: int) -> tuple[dt.date, dt.date]:
    """`days` ending TODAY in the ad account's timezone (see client.account)."""
    days = max(1, min(days, MAX_REPORT_DAYS))
    end, _zone = client.account_today()
    return end - dt.timedelta(days=days - 1), end


def complete_days_window(days: int = 7) -> tuple[dt.date, dt.date]:
    """`days` COMPLETE days ending yesterday, in the account's timezone.

    What a projection divides by 7. A window ending today includes a partial
    day, which understates the daily rate by up to a seventh.
    """
    today, _zone = client.account_today()
    return today - dt.timedelta(days=days), today - dt.timedelta(days=1)


def _json_money(value: Any) -> str:
    """`{"amount": "1.20", "currency": "CAD"}` -> `"1.20 CAD"`."""
    if not isinstance(value, dict):
        return "-"
    amount = value.get("amount")
    if amount in (None, ""):
        return "-"
    return f"{amount} {value.get('currency') or ''}".strip()


def _metrics_row(
    metrics: dict | None, *, id_: Any = None, name: Any = None, extra: dict | None = None
) -> ReportRow:
    metrics = metrics or {}
    return ReportRow(
        id=str(id_) if id_ is not None else None,
        name=name,
        extra={k: str(v) for k, v in (extra or {}).items() if v is not None},
        spend=_json_money(metrics.get("localSpend")),
        impressions=metrics.get("impressions") or 0,
        taps=metrics.get("taps") or 0,
        installs=metrics.get("totalInstalls") or 0,
        new_downloads=metrics.get("totalNewDownloads") or 0,
        redownloads=metrics.get("totalRedownloads") or 0,
        avg_cpt=_json_money(metrics.get("cpt")),
        avg_cpi=_json_money(metrics.get("totalAvgCPI")),
    )


#: Report columns a `sort_by` may name, as Apple spells them in totalMetrics.
REPORT_SORT_FIELDS = ("localSpend", "impressions", "taps", "totalInstalls")
#: Rows per page when an internal sweep reads a whole report.
REPORT_PAGE = 1000
#: Pages a sweep will read before saying it stopped.
REPORT_MAX_PAGES = 20


@dataclasses.dataclass
class ReportPage:
    rows: list[dict]
    grand_total: dict | None
    start: dt.date
    end: dt.date
    #: Apple's count of rows across every page, when it says.
    total: int | None
    truncated: bool = False


def run_report(
    method: str,
    entity: str,
    days: int,
    filters: list[Filter] | None = None,
    granularity: str = "DAILY",
    group_by: list[str] | None = None,
    grand_total: bool = True,
) -> tuple[list[dict], dict | None, dt.date, dt.date]:
    """Every row of one report (all pages), as (rows, grand_total, start, end)."""
    page = run_report_all(method, entity, days, filters, granularity, group_by, grand_total)
    return page.rows, page.grand_total, page.start, page.end


def run_report_page(
    method: str,
    entity: str,
    days: int,
    filters: list[Filter] | None = None,
    granularity: str = "DAILY",
    group_by: list[str] | None = None,
    grand_total: bool = True,
    offset: int = 0,
    page_size: int | None = None,
    sort_by: str | None = None,
    window: tuple[dt.date, dt.date] | None = None,
) -> ReportPage:
    """One page of one `apps_*_reports` call, parsed from RAW JSON.

    RAW JSON rather than the SDK's models is not a style choice.
    apple-ads-platform 1.109.0 generates `ReportingKeyword.status` with the enum
    ('ACTIVE', 'PAUSED', 'DELETED') while the live API returns 'ENABLED', and the
    generated validator RAISES on an unrecognised value instead of falling back
    to its own `unknown_default_open_api` placeholder. The whole keyword report
    therefore fails to deserialize -- a client-side bug, with the response
    sitting there intact. Reports are flattened into ReportRow regardless, so
    nothing is lost by reading the JSON directly.

    PAGINATION. A request with no `pagination` gets Apple's default page and
    nothing says so, so a large report used to be silently cut. Every request
    now says which page it wants, and the caller is told when more exist.

    `_request_timeout` is lifted to 60s: a report is the one read in this API
    that routinely outruns the client's 30s default.
    """
    start, end = window or _window(days)
    response = client.call(
        f"{method}_without_preload_content",
        x_ap_context=context_header(),
        apps_reporting_request=AppsReportingRequest(
            time_range=TimeRange(start=start, end=end, granularity=granularity),
            filters=filters or None,
            group_by=group_by or None,
            options=AppsOptions(include_rows=["GRAND_TOTAL"]) if grand_total else None,
            pagination=RequestPagination(offset=max(0, offset), page_size=page_size or REPORT_PAGE),
            sorting=[Sorting(var_field=sort_by, order="DESC")] if sort_by else None,
        ),
        _request_timeout=REPORT_TIMEOUT,
    )
    try:
        payload = json.loads(response.data.decode("utf-8"))
    except (ValueError, AttributeError, UnicodeDecodeError) as exc:
        raise ToolError(f"INTERNAL: could not parse the {entity} report response: {exc}") from exc

    if payload.get("error"):
        raise ToolError(f"INVALID: Apple rejected the {entity} report request: {payload['error']}")

    result = payload.get("result") or {}
    rows = result.get("rows") or []
    pagination = payload.get("pagination") or result.get("pagination") or {}
    total = pagination.get("totalResults", pagination.get("totalCount"))
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return ReportPage(
        rows=rows,
        grand_total=(result.get("summary") or {}).get("grandTotal"),
        start=start,
        end=end,
        total=total,
    )


def run_report_all(
    method: str,
    entity: str,
    days: int,
    filters: list[Filter] | None = None,
    granularity: str = "DAILY",
    group_by: list[str] | None = None,
    grand_total: bool = True,
    window: tuple[dt.date, dt.date] | None = None,
) -> ReportPage:
    """Every page of a report, up to REPORT_MAX_PAGES, for an internal sweep.

    A projection that looks one keyword up in a campaign report must see the
    whole report: a keyword on page two would otherwise read as "no delivery",
    a confident zero.
    """
    first = run_report_page(
        method, entity, days, filters, granularity, group_by, grand_total, window=window
    )
    rows = list(first.rows)
    page_rows = len(first.rows)
    pages = 1
    while page_rows >= REPORT_PAGE and pages < REPORT_MAX_PAGES:
        if first.total is not None and len(rows) >= first.total:
            break
        nxt = run_report_page(
            method, entity, days, filters, granularity, group_by, False,
            offset=len(rows), window=(first.start, first.end),
        )
        rows.extend(nxt.rows)
        page_rows = len(nxt.rows)
        pages += 1
    truncated = page_rows >= REPORT_PAGE and pages >= REPORT_MAX_PAGES
    return ReportPage(rows, first.grand_total, first.start, first.end, first.total, truncated)


def _spend_of(row: dict, field: str = "localSpend") -> Decimal:
    metrics = row.get("totalMetrics") or {}
    value = metrics.get(field)
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal("0")
    except InvalidOperation:
        return Decimal("0")


def report_rows(
    method: str,
    entity: str,
    days: int,
    filters: list[Filter] | None,
    *,
    narrow,
    limit: int,
    offset: int,
    sort_by: str,
    min_impressions: int,
    grand_total: bool = True,
    group_by: list[str] | None = None,
    granularity: str = "DAILY",
) -> tuple[ReportPage, list[dict], int | None, bool]:
    """(page, rows to show, next_offset, truncated) for a report TOOL.

    Without client-side narrowing, one server-side page sorted by Apple. With it
    (an ad group or keyword id, which Apple's report filters reject), the whole
    report is swept, narrowed, sorted here and sliced -- otherwise the entity
    asked for could sit on a page that was never read.
    """
    if sort_by not in REPORT_SORT_FIELDS:
        raise ToolError(
            f"INVALID_REQUEST: sort_by={sort_by!r} must be one of {', '.join(REPORT_SORT_FIELDS)}"
        )
    if narrow is None:
        page = run_report_page(
            method, entity, days, filters, granularity, group_by, grand_total,
            offset=offset, page_size=limit, sort_by=sort_by,
        )
        rows = [r for r in page.rows if (r.get("totalMetrics") or {}).get("impressions", 0) >= min_impressions]
        seen = offset + len(page.rows)
        more = (page.total is not None and seen < page.total) or (
            page.total is None and len(page.rows) >= limit
        )
        return page, rows, (seen if more else None), more
    page = run_report_all(method, entity, days, filters, granularity, group_by, grand_total)
    rows = [r for r in page.rows if narrow(r)]
    rows = [r for r in rows if (r.get("totalMetrics") or {}).get("impressions", 0) >= min_impressions]
    rows.sort(key=lambda r: _spend_of(r, sort_by), reverse=True)
    sliced = rows[offset: offset + limit]
    more = offset + limit < len(rows) or page.truncated
    page.total = len(rows)
    return page, sliced, (offset + limit if offset + limit < len(rows) else None), more


def report_filter(field: str, values: list[Any]) -> Filter:
    """A reporting filter. Single values use EQUALS, not IN.

    Verified live 2026-10-08: `campaignId filter only supports the EQUALS
    operator for API users. Operator 'IN' is not allowed.` IN is accepted for
    some fields and rejected for others, so sending EQUALS whenever there is one
    value is the shape that works everywhere.
    """
    if len(values) == 1:
        return Filter(field=field, operator="EQUALS", value=values[0])
    return Filter(field=field, operator="IN", value=list(values))


def entity_metrics_7d(
    method: str, id_key: str, entity_id: int, campaign_id: int | None = None
) -> tuple[Decimal, int, int, int, str | None]:
    """(spend, taps, impressions, installs, error) over 7 days for ONE entity.

    Apple's reporting filters accept **campaignId and nothing else**: a keywordId
    or adGroupId filter comes back `INVALID_FIELD_ATTRIBUTE: Filters contain
    unsupported fields`. So the narrowing happens here, in Python, over the rows
    of a campaign-level report -- which is also why campaign_id is required
    rather than optional.

    Failure is NOT fatal to a preview: a preview without 7-day numbers is still
    worth showing, whereas one that refuses to render because reporting hiccuped
    teaches you to skip previews. But it must not be SILENT either -- bare zeros
    make a broken report look exactly like an entity that genuinely spent
    nothing, and the projection built on it then reads 0.00 with total
    confidence. The reason comes back as the fifth element so the preview can
    say so out loud.
    """
    rows, error = report_rows_7d(method, id_key, campaign_id)
    if error:
        return Decimal("0"), 0, 0, 0, error
    return pick_metrics(rows, entity_id)


def report_rows_7d(
    method: str, id_key: str, campaign_id: int | None
) -> tuple[list[dict], str | None]:
    """The raw 7-day report rows one entity's figures are picked out of.

    Split out so a preview touching several entities in one campaign fetches
    the campaign's report ONCE (see changes.context) rather than once per entity.
    """
    # The CAMPAIGN report takes no filter at all -- even campaignId is rejected --
    # so it is fetched whole and the row is picked out. The keyword and ad-group
    # reports take campaignId and nothing else.
    if id_key == "campaignId":
        filters = None
    elif campaign_id is None:
        return [], "no campaign id for this entity, so its report cannot be scoped"
    else:
        filters = [report_filter("campaignId", [campaign_id])]

    try:
        page = run_report_all(
            method, id_key, days=7, filters=filters, window=complete_days_window(7)
        )
    except ToolError as exc:
        return [], str(exc)
    return page.rows, None


def pick_metrics(
    rows: list[dict], entity_id: Any
) -> tuple[Decimal, int, int, int, str | None]:
    """(spend, taps, impressions, installs, None) for one entity's row."""
    metrics = next(
        (
            row.get("totalMetrics")
            for row in rows
            if str((row.get("metadata") or {}).get("id")) == str(entity_id)
        ),
        None,
    )
    if not metrics:
        # The report succeeded and this entity is simply not in it, which over a
        # 7-day window means no delivery. A genuine zero, not a failure.
        return Decimal("0"), 0, 0, 0, None

    raw = (metrics.get("localSpend") or {}).get("amount")
    try:
        spend = Decimal(str(raw)) if raw not in (None, "") else Decimal("0")
    except InvalidOperation:
        spend = Decimal("0")
    return (
        spend,
        metrics.get("taps") or 0,
        metrics.get("impressions") or 0,
        metrics.get("totalInstalls") or 0,
        None,
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
    entity_types: tuple[str, ...] | None = None,
) -> list[Any]:
    """`query_audit_summary` rows over a time window, across every entity type."""
    return query_audit_page(since, limit, until, entity_types)[0]


def query_audit_page(
    since: dt.datetime,
    limit: int = 200,
    until: dt.datetime | None = None,
    entity_types: tuple[str, ...] | None = None,
) -> tuple[list[Any], list[str]]:
    """(rows, entity types whose page came back FULL, so more may exist).

    One page per entity type. A full page used to be silently all there was;
    now the caller is told which types were cut off.

    eventTime takes BETWEEN with a start and an end. GREATER_THAN_OR_EQUAL_TO is
    documented as supported for time fields but answers a bare 400 in practice,
    so a range is what this sends.
    """
    until = until or dt.datetime.now(dt.timezone.utc)
    rows: list[Any] = []
    truncated: list[str] = []
    page_size = min(max(1, limit), 1000)
    for entity_type in entity_types or AUDIT_ENTITY_TYPES:
        response = client.call(
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
                pagination=Pagination(offset=0, page_size=page_size),
            ),
        )
        page = unwrap(response, f"{entity_type} audit summary") or []
        if len(page) >= page_size:
            truncated.append(entity_type)
        rows.extend(page)
    rows.sort(key=lambda r: r.event_time or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    return rows, truncated


# --- report arguments, shared by every report tool -------------------------------
# Module level: the tool modules use `from __future__ import annotations`, so an
# annotation is resolved against these globals, never against register()'s locals.

Days = Annotated[int, Field(description="Window ending today", ge=1, le=90)]
Limit = Annotated[int, Field(description="Max rows to return", ge=1, le=1000)]
Offset = Annotated[int, Field(description="Rows to skip; pass next_offset to page", ge=0)]
SortBy = Annotated[
    str,
    Field(description="Sort descending by: localSpend, impressions, taps or totalInstalls"),
]
MinImpressions = Annotated[
    int, Field(description="Drop rows with fewer impressions than this", ge=0)
]


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
        me = unwrap(client.call("get_me"), "me")
        acls = unwrap(client.call("get_user_acls"), "ACLs")
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
        offset: Annotated[int, Field(description="Rows to skip; pass next_offset to page", ge=0)] = 0,
    ) -> CampaignList:
        """Every campaign, with status, serving status and daily budget.

        Deleted campaigns are dropped AFTER the page is read, so `returned` can
        be under `limit` while next_offset still says more exist.
        """
        rows, total, next_offset = client.query_page("campaigns_query_post", limit=limit, offset=offset)
        if not include_deleted:
            rows = [c for c in rows if not c.deleted]
        return CampaignList(
            total=total, returned=len(rows), campaigns=[_campaign_row(c) for c in rows],
            next_offset=next_offset, truncated=next_offset is not None,
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
        offset: Annotated[int, Field(description="Rows to skip; pass next_offset to page", ge=0)] = 0,
    ) -> AdGroupList:
        """Ad groups, with status, serving status and default bid."""
        filters = [eq_filter("campaignId", campaign_id)] if campaign_id else None
        rows, total, next_offset = client.query_page(
            "adgroups_query_post", filters, limit=limit, offset=offset
        )
        if not include_deleted:
            rows = [g for g in rows if not g.deleted]
        return AdGroupList(
            total=total, returned=len(rows), ad_groups=[_ad_group_row(g) for g in rows],
            next_offset=next_offset, truncated=next_offset is not None,
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
        offset: Annotated[int, Field(description="Rows to skip; pass next_offset to page", ge=0)] = 0,
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
        rows, total, next_offset = client.query_page(
            "keywords_query_post", filters or None, limit=limit, offset=offset
        )
        if not include_deleted:
            rows = [k for k in rows if not k.deleted]
        return KeywordList(
            total=total, returned=len(rows), keywords=[_keyword_row(k) for k in rows],
            next_offset=next_offset, truncated=next_offset is not None,
        )

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
        offset: Annotated[int, Field(description="Rows to skip; pass next_offset to page", ge=0)] = 0,
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
            groups, _cut = client.query_all(
                "adgroups_query_post", [eq_filter("campaignId", campaign_id)], cap=5000
            )
            ad_group_ids = [g.id for g in groups if not g.deleted]

        # Every negative of every ad group, then filtered and sliced: `total` is
        # the real count, not the length of what fits under `limit`.
        rows: list[Any] = []
        swept_all = True
        for group_id in ad_group_ids:
            found, cut = client.query_all(
                "negative_keywords_query_post", [eq_filter("adGroupId", group_id)], cap=5000
            )
            rows.extend(found)
            swept_all = swept_all and not cut

        if not include_deleted:
            rows = [n for n in rows if not n.deleted]
        total = len(rows)
        page = rows[offset: offset + limit]
        next_offset = offset + limit if offset + limit < total else None
        return NegativeKeywordList(
            total=total,
            returned=len(page),
            negative_keywords=[_negative_row(n) for n in page],
            next_offset=next_offset,
            truncated=next_offset is not None or not swept_all,
        )

    # --- reports --------------------------------------------------------------
    #
    # Every report takes the same paging and ordering arguments and says how
    # many rows exist and whether it showed them all. campaignId is the ONLY
    # filter Apple accepts on these reports (an adGroupId or keywordId filter is
    # rejected with INVALID_FIELD_ATTRIBUTE), so ad group and keyword ids narrow
    # the rows here -- after sweeping every page, never just the first.

    def build_report(
        entity: str,
        method: str,
        days: int,
        filters,
        *,
        narrow,
        limit: int,
        offset: int,
        sort_by: str,
        min_impressions: int,
        row: Any,
        grand_total: bool = True,
        note: str = "Totals are for the whole window, not per day.",
    ) -> Report:
        page, rows, next_offset, truncated = report_rows(
            method, entity, days, filters, narrow=narrow, limit=limit, offset=offset,
            sort_by=sort_by, min_impressions=min_impressions, grand_total=grand_total,
        )
        total = page.grand_total if (grand_total and narrow is None) else None
        return Report(
            entity=entity.replace(" ", "_"),
            start=page.start.isoformat(),
            end=page.end.isoformat(),
            granularity="DAILY",
            rows=[row(r) for r in rows],
            grand_total=_metrics_row(total, name="GRAND TOTAL") if total else None,
            returned=len(rows),
            offset=offset,
            next_offset=next_offset,
            total_rows_available=page.total,
            truncated=truncated,
            time_zone=client.account_timezone(),
            note=note,
        )

    def meta(r: dict) -> dict:
        return r.get("metadata") or {}

    @mcp.tool(title="Campaign performance report", annotations=READ_ONLY)
    def campaign_report(
        days: Days = 7,
        campaign_id: Annotated[int | None, Field(description="Limit to one campaign")] = None,
        limit: Limit = 200,
        offset: Offset = 0,
        sort_by: SortBy = "localSpend",
        min_impressions: MinImpressions = 0,
    ) -> Report:
        """Spend, impressions, taps and installs per campaign.

        Field names differ from the v5 API: installs are totalInstalls /
        totalNewDownloads / totalRedownloads, and the bare `installs` key v3 used
        does not exist.
        """
        # The CAMPAIGN report takes no filter at all, so one campaign is picked
        # out of the full report.
        narrow = (lambda r: str(meta(r).get("id")) == str(campaign_id)) if campaign_id else None
        return build_report(
            "campaign", "apps_campaign_reports", days, None, narrow=narrow, limit=limit,
            offset=offset, sort_by=sort_by, min_impressions=min_impressions,
            row=lambda r: _metrics_row(r.get("totalMetrics"), id_=meta(r).get("id"),
                                       name=meta(r).get("name")),
        )

    @mcp.tool(title="Ad group performance report", annotations=READ_ONLY)
    def ad_group_report(
        days: Days = 7,
        campaign_id: Annotated[int | None, Field(description="Limit to one campaign")] = None,
        ad_group_id: Annotated[int | None, Field(description="Limit to one ad group")] = None,
        limit: Limit = 200,
        offset: Offset = 0,
        sort_by: SortBy = "localSpend",
        min_impressions: MinImpressions = 0,
    ) -> Report:
        """Spend, impressions, taps and installs per ad group."""
        filters = [report_filter("campaignId", [campaign_id])] if campaign_id else None
        narrow = (lambda r: str(meta(r).get("id")) == str(ad_group_id)) if ad_group_id else None
        return build_report(
            "ad group", "apps_ad_group_reports", days, filters, narrow=narrow, limit=limit,
            offset=offset, sort_by=sort_by, min_impressions=min_impressions,
            row=lambda r: _metrics_row(
                r.get("totalMetrics"), id_=meta(r).get("id"), name=meta(r).get("name"),
                extra={"campaign_id": meta(r).get("campaignId")},
            ),
        )

    @mcp.tool(title="Keyword performance report", annotations=READ_ONLY)
    def keyword_report(
        days: Days = 7,
        campaign_id: Annotated[int | None, Field(description="Limit to one campaign")] = None,
        ad_group_id: Annotated[
            int | None, Field(description="Narrow to one ad group (filtered after fetching)")
        ] = None,
        keyword_id: Annotated[
            int | None, Field(description="Narrow to one keyword (filtered after fetching)")
        ] = None,
        limit: Limit = 200,
        offset: Offset = 0,
        sort_by: SortBy = "localSpend",
        min_impressions: MinImpressions = 0,
    ) -> Report:
        """Spend and conversions per keyword -- the input to any bid decision.

        Apple only filters these reports by campaignId, so ad_group_id and
        keyword_id narrow the rows after every page has been read. Give
        campaign_id too when you can; without it this pulls every campaign.
        """
        filters = [report_filter("campaignId", [campaign_id])] if campaign_id else None
        narrow = None
        if ad_group_id or keyword_id:
            def narrow(r: dict) -> bool:
                return (not ad_group_id or str(meta(r).get("adGroupId")) == str(ad_group_id)) and (
                    not keyword_id or str(meta(r).get("id")) == str(keyword_id)
                )
        return build_report(
            "keyword", "apps_keyword_reports", days, filters, narrow=narrow, limit=limit,
            offset=offset, sort_by=sort_by, min_impressions=min_impressions,
            row=lambda r: _metrics_row(
                r.get("totalMetrics"),
                id_=meta(r).get("id"),
                name=meta(r).get("text"),
                extra={
                    "match_type": meta(r).get("matchType"),
                    "ad_group_id": meta(r).get("adGroupId"),
                    "ad_group": (meta(r).get("adGroup") or {}).get("name"),
                    "status": meta(r).get("status"),
                    "serving_status": meta(r).get("displayStatus"),
                    "bid": _json_money(meta(r).get("bid")),
                },
            ),
        )

    @mcp.tool(title="Search term report", annotations=READ_ONLY)
    def search_term_report(
        days: Days = 7,
        campaign_id: Annotated[int | None, Field(description="Limit to one campaign")] = None,
        ad_group_id: Annotated[int | None, Field(description="Limit to one ad group")] = None,
        limit: Limit = 200,
        offset: Offset = 0,
        sort_by: SortBy = "localSpend",
        min_impressions: MinImpressions = 0,
    ) -> Report:
        """What people actually typed -- where negative keywords come from.

        A row with no keyword id was served by Search Match, not by one of your
        keywords.
        """
        filters = [report_filter("campaignId", [campaign_id])] if campaign_id else None
        narrow = (
            (lambda r: str(meta(r).get("adGroupId")) == str(ad_group_id)) if ad_group_id else None
        )
        # SEARCHTERM reports accept GRAND_TOTAL *or* granular data, never both:
        # "SEARCHTERM level reports supports only GRAND_TOTAL or granularity data,
        # not both." The per-term rows are the whole point of this report, so the
        # grand total is what gives way.
        return build_report(
            "search term", "apps_search_term_reports", days, filters, narrow=narrow,
            limit=limit, offset=offset, sort_by=sort_by, min_impressions=min_impressions,
            grand_total=False,
            row=lambda r: _metrics_row(
                r.get("totalMetrics"),
                id_=meta(r).get("keywordId"),
                name=meta(r).get("searchTermText") or meta(r).get("searchTerm"),
                extra={
                    "matched_keyword": meta(r).get("keyword") or meta(r).get("text"),
                    "match_type": meta(r).get("matchType") or meta(r).get("searchTermSource"),
                    "ad_group_id": meta(r).get("adGroupId"),
                },
            ),
            note=(
                "A row with no keyword id was served by Search Match, not by a "
                "keyword you set. No grand total: Apple refuses GRAND_TOTAL and "
                "granular rows on the same SEARCHTERM request."
            ),
        )

    # --- recommendations ------------------------------------------------------

    @mcp.tool(title="Apple's daily budget recommendations", annotations=READ_ONLY)
    def budget_recommendations(
        limit: Annotated[int, Field(description="Max recommendations", ge=1, le=200)] = 50,
    ) -> BudgetRecommendationList:
        """What Apple thinks each campaign's daily budget should be. READ ONLY.

        `apply_daily_budget_recommendations` exists in the API and is deliberately
        not exposed: a one-call "apply Apple's advice" would move real money with
        no preview, no bound and no ledger line.
        """
        response = client.call(
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
        rows, truncated = query_audit_page(since, limit)
        cut = len(rows) > limit
        rows = rows[:limit]
        return AuditList(
            returned=len(rows),
            changes=[_audit_row(row) for row in rows],
            truncated=bool(truncated) or cut,
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
        read_only = read_only_mode()
        return GuardrailReport(
            read_only_server=read_only,
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
                "max_applies_per_session": str(limits.max_applies_per_session),
                "max_projected_session_delta": str(limits.max_projected_session_delta),
                "max_applies_per_day": str(limits.max_applies_per_day),
                "max_projected_delta_per_day_total": str(
                    limits.max_projected_delta_per_day_total
                ),
            },
            allowed_campaign_ids=sorted(str(c) for c in limits.allowed_campaign_ids),
            allowed_currencies=sorted(limits.allowed_currencies),
            session={k: str(v) for k, v in state.counters.snapshot().items()},
            daily=DailyUsage(limits).snapshot(),
            pending_previews=state.previews.pending_count(),
            note=(
                "An empty allowed_campaign_ids means every campaign is in scope. "
                "Session counters reset only when the server restarts; the daily "
                "counters come from the ledger, are shared by every process, and "
                "reset at local midnight."
                + (
                    " THIS SERVER IS READ-ONLY: it published no apply_* tools and no "
                    "revert_change, so nothing here can change the account. Previews "
                    "still work -- use them to show a human exactly what you would "
                    "change, and let them make it from a writable client or the Apple "
                    "Ads UI."
                    if read_only
                    else ""
                )
            ),
        )

    @mcp.tool(title="Changes this server has made (the local ledger)", annotations=LOCAL_ONLY)
    def list_my_changes(
        days: Annotated[int, Field(description="How far back to look", ge=1, le=365)] = 7,
        limit: Annotated[int, Field(description="Max entries", ge=1, le=500)] = 100,
    ) -> LedgerList:
        """Every write this server attempted, from the `changes` table.

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
        apple, apple_truncated = query_audit_page(since, limit=1000)
        apple_rows = [_audit_row(row) for row in apple]

        # Join on entity id, which Apple exposes in each summary's `metas`. The
        # summaries are grouped by transaction, so without metas there would be
        # nothing to join on but timestamps -- and a timestamp join would call
        # every change "unexplained" the moment two happened in the same minute.
        apple_ids: set[str] = set()
        for row in apple_rows:
            apple_ids.update(row.entity_ids)

        # A creation's row carries the id Apple assigned (filled in from the
        # response), so it joins like any other. Legacy rows -- a bulk change as
        # one comma-joined row, a negative add as "(new)" -- are split or
        # skipped as before.
        def ids_of(entry: dict[str, Any]) -> set[str]:
            return {
                part.strip()
                for part in str(entry.get("entity_id") or "").split(",")
                if part.strip() and part.strip() not in ("(new)", "None")
            }

        local_ids: set[str] = set()
        for entry in local:
            local_ids |= ids_of(entry)

        local_without_apple = [
            entry
            for entry in local
            if entry.get("outcome") in (ledger.FAILED, None) or not ids_of(entry) & apple_ids
        ]
        apple_without_local = [
            row for row in apple_rows if not (set(row.entity_ids) & local_ids)
        ]

        return ReconcileReport(
            since=since.isoformat(timespec="seconds"),
            local_entries=len(local),
            apple_changes=len(apple_rows),
            apple_truncated=apple_truncated,
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
