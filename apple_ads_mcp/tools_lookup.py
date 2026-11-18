"""Read tools for everything a decision needs beyond the core lists and reports.

The account and its role, ads and creatives, custom product pages, impression
share and search popularity, Apple's suggestions and recommendations, the ids the
targeting and creation writes need (geos, apps, languages), and the field-level
detail behind a change-history row.

Every tool here reads, so every one carries READ_ONLY (all four hints spelled
out -- see tools_read). Several of these endpoints were added for parity and have
not yet been exercised against the live account; their rows are passed through
as Apple's own JSON (`rows: list[dict]`) rather than squeezed into a model that
would silently drop a field nobody predicted. The README's live-check list names
them.

Every list says how many rows exist and whether it showed them all.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from apple_ads_platform import (
    EligibilityQueryRequest,
    Filter,
    GeoEntityType,
    GeoRequest,
    GeoSearchPostRequest,
    ImpressionShareQueryRequest,
    ImpressionShareTimeRange,
    QueryPagination,
    RecommendationFilterCondition,
    RecommendationQueryRequest,
    RecommendationQueryRequestPagination,
    RequestPagination,
    SearchSupplySourceType,
    SearchTermPopularityQueryRequest,
    SearchTermPopularityTimeRange,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from apple_ads_mcp import client
from apple_ads_mcp.client import (
    context_header,
    enum_value,
    eq_filter,
    query,
    unwrap,
)
from apple_ads_mcp.tools_read import READ_ONLY

#: Roles that can change campaigns, as far as a role NAME says. Apple's role
#: names are prose ("API Account Read Only"), so this is a deliberately narrow
#: rule: a role is read-only when its name says "read only" and does not say
#: "write". Anything else counts as able to write, and no roles at all is
#: "unknown", which never blocks.
def role_can_write(roles: list[str]) -> bool | None:
    if not roles:
        return None
    normalised = [str(r).lower().replace("_", " ").replace("-", " ") for r in roles]
    if all("read only" in r and "write" not in r for r in normalised):
        return False
    return True


def account_roles() -> list[str] | None:
    """This credential's roles on the configured ad account, or None if unknown."""
    try:
        acls = unwrap(client.call("get_user_acls"), "ACLs")
    except ToolError:
        return None
    wanted = client.ad_account()
    for acl in getattr(acls, "acls", None) or acls or []:
        account = getattr(acl, "ad_account", None)
        if str(getattr(account, "id", "")) == str(wanted):
            return [str(enum_value(r)) for r in (acl.roles or [])]
    return []


# --- output models ------------------------------------------------------------


class Rows(BaseModel):
    """A list passed through as Apple's own JSON, with paging facts."""

    returned: int
    total: int | None = Field(default=None, description="Rows Apple has, when it says")
    next_offset: int | None = Field(
        default=None, description="Pass as offset for the next page; null on the last"
    )
    truncated: bool = Field(default=False, description="True when more rows exist")
    rows: list[dict[str, Any]]
    note: str = ""


class AccountDetail(BaseModel):
    ad_account_id: str
    name: str | None
    timezone: str | None = Field(description="Every report window is a date in this zone")
    currency: str | None = Field(description="Every amount here is in this currency")
    payment_model: str | None
    system_status: str | None
    system_status_reasons: list[str] = Field(default_factory=list)
    product_features: list[str] = Field(default_factory=list)
    roles: list[str] | None = Field(description="This credential's roles on this account")
    role_can_write: bool | None = Field(
        description="False blocks every preview: the role cannot change campaigns"
    )
    note: str


def dump(model: Any) -> dict[str, Any]:
    """An SDK model as Apple's own JSON keys, without the SDK's catch-all field."""
    if model is None:
        return {}
    if isinstance(model, dict):
        data = dict(model)
    elif hasattr(model, "model_dump"):
        data = model.model_dump(by_alias=True, exclude_none=True, mode="json")
    else:
        return {"value": str(model)}
    data.pop("additional_properties", None)
    return data


def _paged(response: Any, what: str, offset: int, limit: int, note: str = "") -> Rows:
    rows = unwrap(response, what) or []
    if not isinstance(rows, list):
        rows = [rows]
    pagination = getattr(response, "pagination", None)
    total = getattr(pagination, "total_count", None) if pagination is not None else None
    seen = offset + len(rows)
    more = (total is not None and seen < total) or (total is None and len(rows) >= limit)
    return Rows(
        returned=len(rows),
        total=total,
        next_offset=seen if more and rows else None,
        truncated=bool(more and rows),
        rows=[dump(r) for r in rows],
        note=note,
    )


def _recommendation_request(
    filters: dict[str, Any], limit: int, offset: int = 0
) -> RecommendationQueryRequest:
    """Recommendation queries used to be sent with NO filter at all, so the
    answer was whatever Apple chose for the whole account."""
    conditions = [
        RecommendationFilterCondition(
            var_field=field, operator="EQUALS",
            value=[str(v) for v in (value if isinstance(value, list) else [value])],
        )
        for field, value in filters.items()
        if value not in (None, "", [])
    ]
    return RecommendationQueryRequest(
        filters=conditions or None,
        pagination=RecommendationQueryRequestPagination(offset=offset, page_size=limit),
    )


# Module level: this module uses `from __future__ import annotations`, so these
# are resolved against module globals, never against register()'s locals.
Limit = Annotated[int, Field(description="Max rows to return", ge=1, le=1000)]
Offset = Annotated[int, Field(description="Rows to skip; pass next_offset to page", ge=0)]
AdamId = Annotated[str | None, Field(description="Limit to one app (adamId)")]
Campaign = Annotated[int | None, Field(description="Limit to one campaign")]
Country = Annotated[str | None, Field(description="Two-letter country code")]


def register(mcp, state) -> None:  # noqa: C901 -- a flat list of tool definitions
    # --- the account ------------------------------------------------------------

    @mcp.tool(title="The ad account: timezone, currency, status and this user's role",
              annotations=READ_ONLY)
    def get_account() -> AccountDetail:
        """The configured ad account's own record, and the role this credential has.

        Read it first in a session: the timezone decides what "today" means in
        every report, the currency is what every amount is in, and a read-only
        role means every preview will be blocked.
        """
        account = unwrap(
            client.call("ad_accounts_id_get", id=client.ad_account(), x_ap_context=context_header()),
            "ad account",
        )
        roles = account_roles()
        can_write = role_can_write(roles or [])
        return AccountDetail(
            ad_account_id=str(getattr(account, "id", None) or client.ad_account()),
            name=getattr(account, "name", None),
            timezone=getattr(account, "timezone", None),
            currency=getattr(account, "currency", None),
            payment_model=enum_value(getattr(account, "payment_model", None)),
            system_status=enum_value(getattr(account, "system_status", None)),
            system_status_reasons=[str(enum_value(r)) for r in (getattr(account, "system_status_reasons", None) or [])],
            product_features=[str(enum_value(f)) for f in (getattr(account, "product_features", None) or [])],
            roles=roles,
            role_can_write=can_write,
            note=(
                "role_can_write is read from the role NAMES Apple returns; null means "
                "it could not be told, which never blocks a preview."
            ),
        )

    # --- ads, creatives, product pages --------------------------------------------

    @mcp.tool(title="List ads", annotations=READ_ONLY)
    def list_ads(
        ad_group_id: Annotated[int | None, Field(description="Limit to one ad group")] = None,
        campaign_id: Annotated[int | None, Field(description="Limit to one campaign")] = None,
        limit: Limit = 200,
        offset: Offset = 0,
    ) -> Rows:
        """Ads with their status, serving (display) status, creative and the
        system status reasons that explain why one is not serving."""
        filters = []
        if ad_group_id:
            filters.append(eq_filter("adGroupId", ad_group_id))
        if campaign_id:
            filters.append(eq_filter("campaignId", campaign_id))
        response = client.call(
            "ads_query_post", x_ap_context=context_header(),
            query_request=query(page_size=limit, offset=offset, filters=filters or None),
        )
        return _paged(response, "ads", offset, limit)

    @mcp.tool(title="Get one ad by id", annotations=READ_ONLY)
    def get_ad(ad_id: Annotated[int, Field(description="Apple ad id")]) -> dict[str, Any]:
        """One ad: status, display status, system status reasons and creative id."""
        return dump(unwrap(client.call("ads_id_get", id=str(ad_id), x_ap_context=context_header()), "ad"))

    @mcp.tool(title="List creatives (custom product page creatives)", annotations=READ_ONLY)
    def list_creatives(limit: Limit = 200, offset: Offset = 0) -> Rows:
        """Creatives in this account, with their type, product page and system
        status. A creative cannot serve until an ad attaches it to an ad group."""
        response = client.call(
            "creatives_query_post", x_ap_context=context_header(),
            query_request=query(page_size=limit, offset=offset),
        )
        return _paged(response, "creatives", offset, limit)

    @mcp.tool(title="Why Apple rejected an app's creatives", annotations=READ_ONLY)
    def creative_rejection_reasons(
        adam_id: Annotated[str, Field(description="The app's Apple id (adamId)")],
        limit: Limit = 100,
    ) -> Rows:
        """Rejection reasons for an app's product-page assets, if any."""
        from apple_ads_platform import CreativeRejectionReasonQueryRequest

        response = client.call(
            "rejection_reasons_apps_query_post", x_ap_context=context_header(),
            creative_rejection_reason_query_request=CreativeRejectionReasonQueryRequest(
                filters=[eq_filter("adamId", adam_id)],
                pagination=QueryPagination(page_size=limit, offset=0, fetch_total_count=True),
            ),
        )
        return _paged(response, "creative rejection reasons", 0, limit)

    @mcp.tool(title="List an app's custom product pages", annotations=READ_ONLY)
    def list_product_pages(
        adam_id: Annotated[str, Field(description="The app's Apple id (adamId)")],
        limit: Limit = 100,
        offset: Offset = 0,
    ) -> Rows:
        """Custom product pages for an app -- the product_page_id create_creative
        needs. Pages are made in App Store Connect, not here."""
        response = client.call(
            "query_product_pages", x_ap_context=context_header(),
            query_request=query(page_size=limit, offset=offset,
                                filters=[eq_filter("adamId", adam_id)]),
        )
        return _paged(response, "product pages", offset, limit)

    @mcp.tool(title="Get one custom product page, with its locales", annotations=READ_ONLY)
    def get_product_page(
        product_page_id: Annotated[str, Field(description="Product page id")],
        include_locales: Annotated[bool, Field(description="Also read its locale details")] = True,
    ) -> dict[str, Any]:
        """A product page's state and deep link, and optionally each locale's
        name, promotional text and screenshots."""
        page = dump(unwrap(
            client.call("get_product_page_by_id", x_ap_context=context_header(),
                        product_page_id=str(product_page_id)),
            "product page",
        ))
        if include_locales:
            locales = unwrap(
                client.call(
                    "query_product_page_locale_details", x_ap_context=context_header(),
                    query_request=query(page_size=200,
                                        filters=[eq_filter("productPageId", product_page_id)]),
                ),
                "product page locales",
            ) or []
            page["locales"] = [dump(l) for l in locales]
        return page

    # --- auctions ------------------------------------------------------------------

    @mcp.tool(title="Impression share by search term and country", annotations=READ_ONLY)
    def impression_share_report(
        days: Annotated[int, Field(description="Window ending today", ge=1, le=90)] = 7,
        campaign_id: Annotated[int | None, Field(description="Limit to one campaign")] = None,
        country: Annotated[str | None, Field(description="Two-letter country code")] = None,
        limit: Limit = 200,
        offset: Offset = 0,
    ) -> Rows:
        """Share of voice: the impression share range, rank and search popularity
        of each search term, per country. Where you are losing auctions."""
        end, _zone = client.account_today()
        start = end - dt.timedelta(days=days - 1)
        filters = []
        if campaign_id:
            filters.append(Filter(var_field="campaignId", operator="EQUALS", value=str(campaign_id)))
        if country:
            filters.append(Filter(var_field="countryOrRegion", operator="EQUALS", value=country.upper()))
        response = client.call(
            "impression_share_query", x_ap_context=context_header(),
            impression_share_query_request=ImpressionShareQueryRequest(
                filters=filters,
                time_range=ImpressionShareTimeRange(start=start, end=end, granularity="DAILY"),
                pagination=RequestPagination(offset=offset, page_size=limit),
            ),
        )
        return _paged(response, "impression share", offset, limit)

    @mcp.tool(title="How popular a search term is", annotations=READ_ONLY)
    def search_term_popularity(
        terms: Annotated[list[str], Field(description="Search terms to look up")],
        country: Annotated[str, Field(description="Two-letter country code")] = "US",
        days: Annotated[int, Field(description="Window ending today", ge=1, le=90)] = 30,
    ) -> Rows:
        """Apple's popularity score for each search term in one country -- the
        demand behind a keyword before you bid on it."""
        if not terms:
            raise ToolError("INVALID_REQUEST: terms is empty")
        end, _zone = client.account_today()
        start = end - dt.timedelta(days=days - 1)
        response = client.call(
            "search_term_popularity_query", x_ap_context=context_header(),
            search_term_popularity_query_request=SearchTermPopularityQueryRequest(
                filters=[
                    Filter(var_field="searchTerm", operator="IN", value=list(terms)),
                    Filter(var_field="countryOrRegion", operator="EQUALS", value=country.upper()),
                ],
                time_range=SearchTermPopularityTimeRange(
                    start=start.isoformat(), end=end.isoformat(), granularity="DAILY"
                ),
            ),
        )
        return _paged(response, "search term popularity", 0, len(terms))

    # --- suggestions and recommendations ----------------------------------------------

    def suggestions(method: str, what: str, adam_id, campaign_id, country, limit, offset) -> Rows:
        response = client.call(
            method, x_ap_context=context_header(),
            recommendation_query_request=_recommendation_request(
                {"adamId": adam_id, "campaignId": campaign_id, "countryOrRegion": country},
                limit, offset,
            ),
        )
        return _paged(response, what, offset, limit, note="Suggestions only -- nothing is added.")

    @mcp.tool(title="Apple's keyword suggestions", annotations=READ_ONLY)
    def keyword_suggestions(
        adam_id: AdamId = None,
        campaign_id: Campaign = None,
        country: Country = None,
        limit: Annotated[int, Field(description="Max suggestions", ge=1, le=1000)] = 50,
        offset: Offset = 0,
    ) -> Rows:
        """Keywords Apple suggests, narrowed to an app, campaign or country.
        Suggestions only -- add one with preview_add_keywords."""
        return suggestions("query_keyword_suggestions", "keyword suggestions",
                           adam_id, campaign_id, country, limit, offset)

    @mcp.tool(title="Apple's phrase suggestions", annotations=READ_ONLY)
    def phrase_suggestions(
        adam_id: AdamId = None,
        campaign_id: Campaign = None,
        country: Country = None,
        limit: Annotated[int, Field(description="Max suggestions", ge=1, le=1000)] = 50,
        offset: Offset = 0,
    ) -> Rows:
        """Multi-word phrases Apple suggests. Suggestions only."""
        return suggestions("query_phrase_suggestions", "phrase suggestions",
                           adam_id, campaign_id, country, limit, offset)

    @mcp.tool(title="Apple's category suggestions", annotations=READ_ONLY)
    def category_suggestions(
        adam_id: AdamId = None,
        campaign_id: Campaign = None,
        country: Country = None,
        limit: Annotated[int, Field(description="Max suggestions", ge=1, le=1000)] = 50,
        offset: Offset = 0,
    ) -> Rows:
        """App Store categories Apple suggests targeting. Suggestions only."""
        return suggestions("query_category_suggestions", "category suggestions",
                           adam_id, campaign_id, country, limit, offset)

    @mcp.tool(title="Apple's target CPA recommendations", annotations=READ_ONLY)
    def target_cpa_recommendations(
        campaign_id: Campaign = None,
        limit: Annotated[int, Field(description="Max recommendations", ge=1, le=200)] = 50,
    ) -> Rows:
        """What Apple thinks each campaign's CPA goal should be. READ ONLY: act on
        one with preview_target_cpa_recommendation, which runs the normal CPA goal
        bounds."""
        response = client.call(
            "query_target_cpa_recommendations", x_ap_context=context_header(),
            recommendation_query_request=_recommendation_request({"campaignId": campaign_id}, limit),
        )
        return _paged(response, "target CPA recommendations", 0, limit)

    @mcp.tool(title="Apple's suggested target CPA for an app and country", annotations=READ_ONLY)
    def target_cpa_suggestion(
        adam_id: Annotated[str, Field(description="The app's Apple id (adamId)")],
        country: Country = None,
    ) -> Rows:
        """A starting CPA goal for a new ad group, by app and country."""
        response = client.call(
            "query_target_cpa_suggestion", x_ap_context=context_header(),
            recommendation_query_request=_recommendation_request(
                {"adamId": adam_id, "countryOrRegion": country}, 50
            ),
        )
        return _paged(response, "target CPA suggestion", 0, 50)

    # --- the ids the targeting and creation writes need ----------------------------------

    @mcp.tool(title="Search locations (countries, regions, cities, postal codes)",
              annotations=READ_ONLY)
    def search_geos(
        query_text: Annotated[str, Field(description="Place name to search for, e.g. Springfield")],
        entity: Annotated[
            str | None, Field(description="Country, AdminArea, Locality or PostalCode")
        ] = None,
        country_code: Annotated[str | None, Field(description="Two-letter country to search in")] = None,
        limit: Limit = 50,
        offset: Offset = 0,
    ) -> Rows:
        """The location ids preview_ad_group_locations needs."""
        kwargs: dict[str, Any] = {
            "x_ap_context": context_header(),
            "supply_source": SearchSupplySourceType("APPSTORE"),
            "query": query_text,
            "offset": offset,
            "page_size": limit,
        }
        if entity:
            kwargs["entity"] = GeoEntityType(entity)
        if country_code:
            kwargs["countrycode"] = country_code.upper()
        return _paged(client.call("search_geos", **kwargs), "geo search", offset, limit)

    @mcp.tool(title="Look up locations by id", annotations=READ_ONLY)
    def get_geos(
        ids: Annotated[list[str], Field(description="Location ids, e.g. from search_geos")],
        entity: Annotated[str, Field(description="Country, AdminArea, Locality or PostalCode")],
    ) -> Rows:
        """Names for location ids -- what an ad group's targeting ids mean."""
        response = client.call(
            "get_geos_by_ids", x_ap_context=context_header(),
            geo_search_post_request=GeoSearchPostRequest(
                geo_request=[GeoRequest(id=str(i), entity=GeoEntityType(entity)) for i in ids],
                supply_source=SearchSupplySourceType("APPSTORE"),
            ),
        )
        return _paged(response, "geos", 0, max(1, len(ids)))

    @mcp.tool(title="List saved location groups", annotations=READ_ONLY)
    def list_location_groups(limit: Limit = 100, offset: Offset = 0) -> Rows:
        """Saved location sets. Read only: creating or editing one needs a brand
        (Maps) context this server does not cover."""
        response = client.call(
            "query_location_groups", x_ap_context=context_header(),
            query_request=query(page_size=limit, offset=offset),
        )
        return _paged(response, "location groups", offset, limit)

    @mcp.tool(title="Search App Store apps (yours, or any)", annotations=READ_ONLY)
    def search_apps(
        query_text: Annotated[str, Field(description="App name to search for")],
        owned_only: Annotated[bool, Field(description="Only apps this account can promote")] = True,
        limit: Limit = 20,
        offset: Offset = 0,
    ) -> Rows:
        """App ids (adamId) -- what create_campaign promotes."""
        response = client.call(
            "search_apps", x_ap_context=context_header(), query=query_text,
            return_owned_apps=owned_only, offset=offset, limit=limit,
        )
        return _paged(response, "app search", offset, limit)

    @mcp.tool(title="Where an app can be advertised, and its details", annotations=READ_ONLY)
    def app_eligibility(
        adam_id: Annotated[str, Field(description="The app's Apple id (adamId)")],
    ) -> dict[str, Any]:
        """The app's details and its eligibility per placement, country and device
        -- check this before create_campaign."""
        details = unwrap(
            client.call("get_app_details_by_adam_id", x_ap_context=context_header(),
                        adam_id=int(adam_id)),
            "app details",
        )
        eligibility = unwrap(
            client.call(
                "eligibilities_apps_query_post", x_ap_context=context_header(),
                eligibility_query_request=EligibilityQueryRequest(
                    filters=[eq_filter("adamId", adam_id)],
                    pagination=QueryPagination(page_size=1000, offset=0, fetch_total_count=True),
                ),
            ),
            "app eligibility",
        ) or []
        return {
            "app": dump(details),
            "eligibility": [dump(e) for e in (eligibility if isinstance(eligibility, list) else [eligibility])],
        }

    @mcp.tool(title="Languages an app's product page supports", annotations=READ_ONLY)
    def supported_languages(
        adam_id: Annotated[str, Field(description="The app's Apple id (adamId)")],
    ) -> Rows:
        """The locales an app supports, per country."""
        response = client.call(
            "query_supported_app_languages", x_ap_context=context_header(),
            query_request=query(page_size=200, filters=[eq_filter("adamId", adam_id)]),
        )
        return _paged(response, "supported languages", 0, 200)

    # --- change history detail -------------------------------------------------------------

    @mcp.tool(title="Field-level detail of one change in Apple's history", annotations=READ_ONLY)
    def get_change_details(
        detail_id: Annotated[
            str, Field(description="A detail_ids value from list_recent_changes")
        ],
        limit: Annotated[int, Field(description="Max fields", ge=1, le=1000)] = 100,
    ) -> Rows:
        """Which fields changed, from what to what, for one list_recent_changes row."""
        response = client.call(
            "get_change_details", x_ap_context=context_header(), detail_id=detail_id,
            limit=limit, offset=0,
        )
        return _paged(response, "change details", 0, limit)

    # --- shared budgets ----------------------------------------------------------------------

    @mcp.tool(title="List shared budgets, and the campaigns each one feeds", annotations=READ_ONLY)
    def list_shared_budgets(limit: Limit = 100, offset: Offset = 0) -> Rows:
        """Shared budgets in this account. READ ONLY -- there is no write tool.

        `shared_budgets_id_put` takes no account context, and its update can move
        a budget between ad accounts, so editing one is a job for the Apple Ads UI.
        Each row adds `campaign_ids`: the campaigns assigned to it.
        """
        response = client.call(
            "shared_budgets_query_post", x_ap_context=context_header(),
            query_request=query(page_size=limit, offset=offset),
        )
        result = _paged(response, "shared budgets", offset, limit)
        feeds: dict[str, list[str]] = {}
        try:
            campaigns, _cut = client.query_all("campaigns_query_post", None, cap=5000)
        except ToolError:
            campaigns = []
        for campaign in campaigns:
            for assignment in getattr(campaign, "shared_budgets", None) or []:
                feeds.setdefault(str(getattr(assignment, "budget_id", "")), []).append(str(campaign.id))
        for row in result.rows:
            row["campaign_ids"] = feeds.get(str(row.get("id")), [])
            value = row.get("value")
            if isinstance(value, dict) and value.get("amount") not in (None, ""):
                row["value_text"] = f"{value['amount']} {value.get('currency') or ''}".strip()
        result.note = (
            "Read only. shared_budgets_id_put takes no account context in this SDK "
            "version, so no write tool is exposed for it."
        )
        return result
