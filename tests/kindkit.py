"""A test rig for change kinds: real SDK models, a fake Apple, nothing sent.

`FakeApple` holds a small account as real `apple_ads_platform` models and
answers the SDK method NAMES the server calls through `client.call`. Request
bodies arrive as the real SDK request models the kinds built -- so a misspelt
field fails here exactly as it would in production -- and are applied to the
fake account so a read-back sees them.

Every id, name and text here is a placeholder. This repository is public.

Not a test module (no ``test_`` prefix). Imported by the kind tests.
"""

from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import apple_ads_platform as sdk  # noqa: E402

from apple_ads_mcp import client, logdb, tools_write  # noqa: E402
from apple_ads_mcp.guardrails import Limits, SessionCounters  # noqa: E402
from apple_ads_mcp.previews import PreviewStore  # noqa: E402

CAMPAIGN = 1111
AD_GROUP = 2222
KEYWORD = 3333
NEGATIVE = 4444
AD = 5555
CREATIVE = 6666
APP = "1234567890"

_TEMP_DIRS: list[tempfile.TemporaryDirectory] = []

#: The real `client.call`, captured before any test replaces it with a fake.
REAL_CALL = client.call


def no_live_client() -> None:
    """Make building a real API client impossible for the rest of the process."""

    def refuse():
        raise RuntimeError("tests must never build a live Apple Ads client")

    client._build = refuse
    client._ad_account_id = "1234567890"
    client._account = None


def temp_db() -> None:
    tmp = tempfile.TemporaryDirectory()
    _TEMP_DIRS.append(tmp)
    logdb.LOG_DIR = pathlib.Path(tmp.name) / "logs"
    logdb.DB_PATH = logdb.LOG_DIR / "apple-ads.db"
    logdb._local = threading.local()
    logdb._session_id = None


def money(amount: str, currency: str = "USD") -> dict:
    return {"amount": amount, "currency": currency}


class _Raw:
    """What a `*_without_preload_content` call returns: bytes in `.data`."""

    def __init__(self, payload: dict) -> None:
        self.data = json.dumps(payload).encode()


class _Resp:
    def __init__(self, result: Any, pagination: Any = None) -> None:
        self.result = result
        self.error = None
        self.pagination = pagination


class FakeApple:
    """A tiny Apple Ads account. Call it like `client.call`."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        #: When True, writes answer 200 and change nothing (an automated bid
        #: strategy does this to bids).
        self.ignore_writes = False
        #: Method name -> exception to raise instead of answering.
        self.fail: dict[str, Exception] = {}
        self.campaigns: dict[int, sdk.Campaign] = {}
        self.ad_groups: dict[int, sdk.AdGroup] = {}
        self.keywords: dict[int, sdk.Keyword] = {}
        self.negatives: dict[int, sdk.NegativeKeyword] = {}
        self.ads: dict[int, sdk.Ad] = {}
        self.creatives: dict[int, sdk.Creative] = {}
        #: Report rows keyed by report method: [{"metadata": {...}, "totalMetrics": {...}}]
        self.reports: dict[str, list[dict]] = {}
        self.extra: dict[str, Any] = {}
        self._next_id = 90000
        self.seed()

    # -- the account -------------------------------------------------------------

    def seed(self) -> None:
        self.campaigns[CAMPAIGN] = sdk.Campaign.model_validate({
            "id": CAMPAIGN, "name": "Example Campaign", "status": "ENABLED",
            "displayStatus": "RUNNING", "adAccountId": 1234567890,
            "dailyBudget": {"value": money("100.00")},
            "promotedObjectId": APP, "promotedObjectType": "APPSTORE_APP",
            "billingEvent": "TAPS",
            "targeting": {"countryOrRegion": {"include": ["US"]},
                          "supplyPlacement": {"include": ["APPSTORE_SEARCH_RESULTS"]}},
        })
        self.ad_groups[AD_GROUP] = sdk.AdGroup.model_validate({
            "id": AD_GROUP, "campaignId": CAMPAIGN, "name": "Example Ad Group",
            "status": "ENABLED", "displayStatus": "RUNNING", "pricingModel": "CPT",
            "automatedKeywordsOptIn": False,
            "bidStrategy": {"bidStrategyType": "MANUAL_CPT", "bidStrategyGoal": "TAP",
                            "bid": money("1.00")},
            "targeting": {"deviceClass": {"include": ["IPHONE", "IPAD"]}},
        })
        self.keywords[KEYWORD] = sdk.Keyword.model_validate({
            "id": KEYWORD, "campaignId": CAMPAIGN, "adGroupId": AD_GROUP,
            "text": "example term", "matchType": "EXACT", "bid": money("1.00"),
            "status": "ENABLED", "displayStatus": "RUNNING",
        })
        self.negatives[NEGATIVE] = sdk.NegativeKeyword.model_validate({
            "id": NEGATIVE, "campaignId": CAMPAIGN, "adGroupId": AD_GROUP,
            "text": "free", "matchType": "BROAD", "status": "ENABLED",
        })
        self.ads[AD] = sdk.Ad.model_validate({
            "id": AD, "campaignId": CAMPAIGN, "adGroupId": AD_GROUP, "creativeId": CREATIVE,
            "name": "Example Ad", "status": "ENABLED", "displayStatus": "RUNNING",
        })
        self.creatives[CREATIVE] = sdk.Creative.model_validate({
            "id": CREATIVE, "name": "Example Creative", "creativeType": "CUSTOM_PRODUCT_PAGE",
        })
        self.reports = {
            "apps_campaign_reports": [self.report_row(CAMPAIGN, spend="70.00", taps=70)],
            "apps_ad_group_reports": [self.report_row(AD_GROUP, spend="70.00", taps=70)],
            "apps_keyword_reports": [self.report_row(KEYWORD, spend="35.00", taps=35)],
            "apps_ad_reports": [self.report_row(AD, spend="14.00", taps=14)],
            "apps_search_term_reports": [],
        }

    @staticmethod
    def report_row(entity_id: int, spend: str, taps: int, **metadata: Any) -> dict:
        return {
            "metadata": {"id": entity_id, "name": f"entity {entity_id}", **metadata},
            "totalMetrics": {"localSpend": money(spend), "taps": taps, "impressions": taps * 10,
                             "totalInstalls": taps // 7},
        }

    def new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    # -- dispatch ----------------------------------------------------------------

    def __call__(self, method: str, /, **kwargs: Any) -> Any:
        kwargs.pop("_request_timeout", None)
        self.calls.append((method, kwargs))
        if method in self.fail:
            raise self.fail[method]
        if method.endswith("_without_preload_content"):
            base = method.removesuffix("_without_preload_content")
            rows = list(self.reports.get(base, []))
            return _Raw({"result": {"rows": rows, "summary": {}}, "pagination": {"totalResults": len(rows)}})
        handler = getattr(self, f"_{method}", None)
        if handler is None:
            if method in self.extra:
                value = self.extra[method]
                return value(**kwargs) if callable(value) else value
            raise AssertionError(f"FakeApple has no answer for {method}")
        return handler(**kwargs)

    def writes(self) -> list[str]:
        reads = ("_get", "_query_post", "query_", "get_", "search_", "_reports")
        return [
            m for m, _ in self.calls
            if not any(m.endswith(r) or m.startswith(r) for r in reads)
            and not m.endswith("_without_preload_content")
        ]

    # -- reads -------------------------------------------------------------------

    def _get(self, store: dict, id: str, what: str):
        entity = store.get(int(id))
        if entity is None:
            from mcp.server.mcpserver.exceptions import ToolError
            raise ToolError(f"NOT_FOUND: no {what} {id}")
        return _Resp(entity.model_copy(deep=True))

    def _campaigns_id_get(self, id, **_):
        return self._get(self.campaigns, id, "campaign")

    def _adgroups_id_get(self, id, **_):
        return self._get(self.ad_groups, id, "ad group")

    def _keywords_id_get(self, id, **_):
        return self._get(self.keywords, id, "keyword")

    def _negative_keywords_id_get(self, id, **_):
        return self._get(self.negatives, id, "negative keyword")

    def _ads_id_get(self, id, **_):
        return self._get(self.ads, id, "ad")

    def _creatives_id_get(self, id, **_):
        return self._get(self.creatives, id, "creative")

    def _query(self, store: dict, query_request) -> _Resp:
        rows = list(store.values())
        for f in (query_request.filters or []) if query_request else []:
            field = {"adGroupId": "ad_group_id", "campaignId": "campaign_id"}.get(f.var_field)
            if field:
                wanted = {str(v) for v in (f.value or [])}
                rows = [r for r in rows if str(getattr(r, field, None)) in wanted]
        page = query_request.pagination if query_request else None
        offset = (page.offset or 0) if page else 0
        size = (page.page_size or 1000) if page else 1000
        chunk = rows[offset: offset + size]
        return _Resp(chunk, SimpleNamespace(total_results=len(rows), total_count=len(rows),
                                            start_index=offset, items_per_page=size))

    def _campaigns_query_post(self, query_request=None, **_):
        return self._query(self.campaigns, query_request)

    def _adgroups_query_post(self, query_request=None, **_):
        return self._query(self.ad_groups, query_request)

    def _keywords_query_post(self, query_request=None, **_):
        return self._query(self.keywords, query_request)

    def _negative_keywords_query_post(self, query_request=None, **_):
        return self._query(self.negatives, query_request)

    def _ads_query_post(self, query_request=None, **_):
        return self._query(self.ads, query_request)

    def _creatives_query_post(self, query_request=None, **_):
        return self._query(self.creatives, query_request)

    def _ad_accounts_id_get(self, id, **_):
        return _Resp(sdk.AdAccount.model_validate(
            {"id": int(id), "name": "Example Account", "timezone": "America/New_York",
             "currency": "USD"}
        ))

    # -- writes ------------------------------------------------------------------

    def _put(self, store: dict, id: str, update) -> _Resp:
        entity = store[int(id)]
        if not self.ignore_writes:
            data = entity.model_dump(by_alias=True, exclude_none=True)
            changes = update.model_dump(by_alias=True, exclude_none=True)
            for key, value in changes.items():
                if isinstance(value, dict) and isinstance(data.get(key), dict):
                    data[key] = {**data[key], **value}
                else:
                    data[key] = value
            store[int(id)] = type(entity).model_validate(data)
        return _Resp(store[int(id)].model_copy(deep=True))

    def _campaigns_id_put(self, id, campaign_update, **_):
        return self._put(self.campaigns, id, campaign_update)

    def _adgroups_id_put(self, id, ad_group_update, **_):
        return self._put(self.ad_groups, id, ad_group_update)

    def _keywords_id_put(self, id, keyword_update, **_):
        return self._put(self.keywords, id, keyword_update)

    def _negative_keywords_id_put(self, id, negative_keyword_update, **_):
        return self._put(self.negatives, id, negative_keyword_update)

    def _ads_id_put(self, id, ad_update, **_):
        return self._put(self.ads, id, ad_update)

    def _creatives_id_put(self, id, creative_update, **_):
        return self._put(self.creatives, id, creative_update)

    def _bulk(self, items, apply) -> _Resp:
        results = []
        for item in items:
            try:
                results.append(SimpleNamespace(
                    correlation_id=item.correlation_id, success=True,
                    result=apply(item.data), error=None,
                ))
            except KeyError as exc:
                results.append(SimpleNamespace(
                    correlation_id=item.correlation_id, success=False, result=None,
                    error=f"not found: {exc}",
                ))
        return _Resp(results)

    def _keywords_bulk_update_post(self, keyword_update_bulk_request, **_):
        def apply(data):
            return self._put(self.keywords, data.id, sdk.KeywordUpdate(
                bid=data.bid, status=getattr(data, "status", None))).result
        return self._bulk(keyword_update_bulk_request.items, apply)

    def _keywords_bulk_create_post(self, keyword_create_bulk_request, **_):
        def apply(data):
            new = sdk.Keyword.model_validate({
                "id": self.new_id(), "adGroupId": data.ad_group_id,
                "campaignId": self.ad_groups[data.ad_group_id].campaign_id,
                "text": data.text, "matchType": data.match_type.value if data.match_type else "BROAD",
                "bid": data.bid.model_dump() if data.bid else None,
                "status": data.status.value if data.status else "ENABLED",
            })
            self.keywords[new.id] = new
            return new
        return self._bulk(keyword_create_bulk_request.items, apply)

    def _negative_keywords_bulk_create_post(self, negative_keyword_create_bulk_request, **_):
        def apply(data):
            new = sdk.NegativeKeyword.model_validate({
                "id": self.new_id(), "adGroupId": data.ad_group_id, "campaignId": data.campaign_id,
                "text": data.text, "matchType": data.match_type.value,
                "status": data.status.value if data.status else "ENABLED",
            })
            self.negatives[new.id] = new
            return new
        return self._bulk(negative_keyword_create_bulk_request.items, apply)

    def _negative_keywords_bulk_update_post(self, negative_keyword_update_bulk_request, **_):
        def apply(data):
            return self._put(self.negatives, data.id,
                             sdk.NegativeKeywordUpdate(status=data.status)).result
        return self._bulk(negative_keyword_update_bulk_request.items, apply)


class Registrar:
    """Collects tool functions by name, as the MCP server would register them."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, **_kw):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorate


def limits(**over) -> Limits:
    base = dict(
        max_bid=Decimal("5.00"), max_bid_increase_pct=50.0,
        max_daily_budget=Decimal("150.00"), max_budget_change_pct=25.0,
        max_entities_per_call=25, max_projected_delta_per_day=Decimal("50.00"),
        max_applies_per_session=20, max_projected_session_delta=Decimal("150.00"),
        allowed_currencies=frozenset({"USD"}),
    )
    base.update(over)
    return Limits(**base)


def rig(**limit_overrides):
    """(tools, apple, state): every write tool wired to a fresh FakeApple and DB."""
    no_live_client()
    temp_db()
    apple = FakeApple()
    client.call = apple
    tools_write.call = apple
    tools_write.writes_disabled = lambda: None
    lim = limits(**limit_overrides)
    state = SimpleNamespace(limits=lim, counters=SessionCounters(lim), previews=PreviewStore())
    registrar = Registrar()
    tools_write.register(registrar, state)
    return registrar.tools, apple, state


def failed(preview, name: str) -> bool:
    return any(line.startswith("FAIL") and f" {name}:" in line for line in preview.guardrail_checks)


def run(tests: dict) -> int:
    """Script entry point shared by the kind test modules."""
    failures = 0
    for name, test in sorted(tests.items()):
        if not name.startswith("test_"):
            continue
        try:
            test()
            print(f"  PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {name}: {exc}")
        except Exception as exc:  # a crash is a failure too
            failures += 1
            print(f"  ERROR {name}: {type(exc).__name__}: {exc}")
    print("All checks passed." if not failures else f"{failures} failure(s).")
    return 1 if failures else 0
