"""One Platform API client for the server's lifetime, plus entity lookups.

Three things live here:

1. `get_api()` -- builds `AppleAdsApi` once, lazily, behind a lock. Lazily
   because `build()` constructs a TokenManager that fetches a token eagerly:
   building at import time would mean a server that refuses to start when the
   network is down, rather than one whose first tool call reports a network
   problem.

2. Timeouts. The SDK's default is 5.0s, which is fine for a GET and far too
   short for a report. They are split per operation, and writes get the SHORTEST
   one on purpose -- see WRITE_TIMEOUT.

3. Entity fetch and name resolution. `describe_*` returns the readable
   `Campaign 'X' > AdGroup 'Y' > Keyword 'z'` path that makes a preview
   reviewable; an id on its own is not something a human can approve.

Token refresh needs no help from us. `EphemeralClientSecretProvider.create_secret()`
is called on every token fetch, so the 180-day client-secret JWT is re-derived
rather than signed once at build -- a server running for months cannot age out of
its own secret. The retry in `call()` is for the other case: a token revoked or
invalidated server-side mid-session.
"""

from __future__ import annotations

import threading
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, TypeVar

from apple_ads_platform import (
    ApiException,
    Money,
    QueryFilter,
    QueryPagination,
    QueryRequest,
)
from apple_ads_platform.auth.errors import AuthError
from apple_ads_platform.builder import AppleAdsClientBuilder
from mcp.server.mcpserver.exceptions import ToolError

from apple_ads_mcp import logdb
from apple_ads_mcp.config import load_config, private_key_pem, resolve_ad_account_id

# Read calls are cheap and chatty; reports are neither.
READ_TIMEOUT = 30.0
REPORT_TIMEOUT = 60.0

# Writes get the SHORTEST timeout, which looks backwards and is not. A write that
# has not answered in 10s has either landed or not, and waiting longer tells you
# nothing either way -- what tells you is the `intent` ledger line, which was
# already written before the call left the process. A short timeout returns
# control to a human sooner; it does not cancel anything at Apple's end.
WRITE_TIMEOUT = 10.0

# Apple caps page size; this is a sane ceiling for a tool response in any case.
MAX_PAGE_SIZE = 1000

_api = None
_api_lock = threading.Lock()
_ad_account_id: str | None = None

T = TypeVar("T")


def context_header(ad_account_id: str | None = None) -> str:
    """`adAccountId=<id>;` -- the trailing semicolon is Apple's format, not a typo.

    v5 used `orgId=<id>` with no semicolon. Getting this wrong is a 401/403 on
    every call that carries it, while `get_user_acls()` keeps working, because
    that is the one endpoint that takes no context at all.
    """
    return f"adAccountId={ad_account_id or ad_account()};"


def ad_account() -> str:
    global _ad_account_id
    if _ad_account_id is None:
        _ad_account_id = resolve_ad_account_id()
    return _ad_account_id


def get_api():
    """The shared `AppleAdsApi`. Thread-safe: tools run on anyio worker threads."""
    global _api
    if _api is None:
        with _api_lock:
            if _api is None:
                _api = _build()
    return _api


def _build():
    config = load_config()
    return (
        AppleAdsClientBuilder.from_private_key(
            client_id=config["APPLE_ADS_CLIENT_ID"],
            team_id=config["APPLE_ADS_TEAM_ID"],
            key_id=config["APPLE_ADS_KEY_ID"],
            private_key=private_key_pem(),
        )
        .api_timeout(READ_TIMEOUT)
        .build()
    )


def reset_api() -> None:
    """Drop the cached client so the next call rebuilds it (and re-authenticates)."""
    global _api
    with _api_lock:
        _api = None


def call(method_name: str, /, **kwargs: Any) -> Any:
    """Invoke one `AppleAdsApi` method, with auth retry and readable failures.

    Takes the method NAME rather than a bound callable so the client is rebuilt
    between the attempt and the retry -- a retry through a stale bound method
    would just re-run against the client that already failed.

    Every exception leaving here is a ToolError, because that is the only
    exception type the MCP SDK renders back to the model verbatim; anything else
    becomes an opaque UnexpectedToolError and the model is told nothing useful.

    This is the chokepoint every caller shares -- the MCP tools and the fetch
    scripts both -- so it is also where each outbound call is logged. A 401
    retry is logged as a second row, because it IS a second request to Apple
    and a log that hid it would understate what the account actually saw.
    """
    try:
        return _invoke(method_name, kwargs)
    except ApiException as exc:
        if exc.status in (401, 403):
            # Could be an expired/revoked token, could be the context header. One
            # rebuild distinguishes them: if it was the header, this fails again.
            reset_api()
            try:
                return _invoke(method_name, kwargs)
            except ApiException as retry_exc:
                raise ToolError(_describe(method_name, retry_exc)) from retry_exc
        raise ToolError(_describe(method_name, exc)) from exc
    except AuthError as exc:
        raise ToolError(f"Apple rejected our credentials: {exc}") from exc
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"{method_name} failed to reach Apple: {type(exc).__name__}: {exc}") from exc


def _invoke(method_name: str, kwargs: dict[str, Any]) -> Any:
    """One attempt at one SDK method, logged to the database.

    Separated from `call()` so the 401 rebuild-and-retry produces two rows
    rather than one; the logger itself never raises, so a failure here is the
    API's, not the log's.
    """
    with logdb.record_api_call(method_name, request=kwargs) as slot:
        try:
            return getattr(get_api(), method_name)(**kwargs)
        except ApiException as exc:
            slot["http_status"] = getattr(exc, "status", None)
            raise


def _describe(method_name: str, exc: ApiException) -> str:
    body = str(getattr(exc, "body", "") or "")[:600]
    hint = ""
    if exc.status in (401, 403):
        hint = (
            " (a 401/403 on a call that carries X-AP-Context usually means the "
            "header or the adAccountId is wrong, not that the token is bad)"
        )
    return f"Apple returned HTTP {exc.status} for {method_name}{hint}: {exc.reason} {body}".strip()


def unwrap(response: Any, what: str) -> Any:
    """Return `response.result`, turning an in-body `error` into a ToolError.

    The Platform API can answer HTTP 200 with an `error` object in the body, so a
    caller that only checks for exceptions will happily read `result=None` as
    "no rows" when it actually means "the request was rejected".
    """
    error = getattr(response, "error", None)
    if error is not None:
        raise ToolError(f"Apple rejected the {what} request: {error}")
    return getattr(response, "result", None)


# --- small conversions -------------------------------------------------------


def enum_value(value: Any) -> str | None:
    """`CampaignStatus.ENABLED` -> `'ENABLED'`. None stays None."""
    if value is None:
        return None
    return getattr(value, "value", value)


def is_unknown_enum(value: Any) -> bool:
    """True for the generated client's forward-compatibility placeholder.

    The SDK deserializes a status it does not recognise to
    `unknown_default_open_api` rather than raising. Tolerable on a read; NOT
    tolerable as the `before` state of a write, because there is no honest way to
    describe, or to revert to, a value we could not parse.
    """
    return enum_value(value) == "unknown_default_open_api"


def money_amount(value: Any) -> Decimal | None:
    """The numeric part of a `Money`, as a Decimal. None when absent."""
    if value is None:
        return None
    raw = getattr(value, "amount", None)
    if raw in (None, ""):
        return None
    try:
        return Decimal(str(raw))
    except InvalidOperation:
        return None


def money_currency(value: Any) -> str | None:
    if value is None:
        return None
    return getattr(value, "currency", None)


def format_money(value: Any) -> str:
    """`'1.20 CAD'` -- what a human reads in an approval prompt."""
    amount = money_amount(value)
    if amount is None:
        return "-"
    currency = money_currency(value) or ""
    return f"{amount} {currency}".strip()


def normalized_value(value: str) -> str:
    """Compare "0.01 CAD" with "0.01" by amount, and statuses by text.

    Apple echoes money back with a currency and sometimes with different
    trailing zeros ("0.0100"), so a string comparison would report a successful
    write as a failure.
    """
    head = str(value).strip().split(" ")[0]
    try:
        return str(Decimal(head).normalize())
    except Exception:
        return str(value).strip().upper()


def landed(observed: str | None, intended: str) -> bool:
    """Did the value Apple echoed back actually become the one we asked for?

    HTTP 200 DOES NOT MEAN APPLIED. Verified live 2026-10-08: a keyword-bid PUT
    against an ad group on an automated bid strategy returns 200 with the entity
    unchanged and nothing in the response saying so. Trusting the status code
    reports a successful change that never happened -- and the ledger then
    records an `after` the account never held, which makes every later revert
    and reconcile wrong too.

    Lives here rather than inside tools_write so the ledger migration can
    classify a historical entry by the SAME rule the live write path uses,
    rather than by a second copy of it free to drift.
    """
    if observed is None:
        return False  # nothing echoed back is not evidence of success
    return normalized_value(observed) == normalized_value(intended)


def parse_decimal(raw: str, field: str) -> Decimal:
    """Money arrives as a decimal STRING, never as cents-as-int.

    An integer argument is the one mistake here that is silently 100x wrong, and
    the value is rendered verbatim in the permission prompt a human approves.
    """
    text = str(raw).strip()
    if not text:
        raise ToolError(f"{field} is empty; give an amount like \"1.20\"")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ToolError(
            f"{field}={raw!r} is not a decimal amount. Write money as a decimal "
            f'string in major units, e.g. "1.20" for one dollar twenty -- never '
            f"cents as an integer."
        ) from None
    if value.is_signed() or not value.is_finite():
        raise ToolError(f"{field}={raw!r} must be a finite, non-negative amount")
    if -value.as_tuple().exponent > 2:
        raise ToolError(f"{field}={raw!r} has more than 2 decimal places")
    return value


def to_money(amount: Decimal | str, currency: str | None) -> Money:
    return Money(amount=str(amount), currency=currency)


# --- queries -----------------------------------------------------------------


def query(page_size: int = 100, offset: int = 0, filters: list[QueryFilter] | None = None):
    return QueryRequest(
        filters=filters or None,
        pagination=QueryPagination(
            page_size=min(max(1, page_size), MAX_PAGE_SIZE),
            offset=max(0, offset),
            fetch_total_count=True,
        ),
    )


def eq_filter(field: str, value: Any) -> QueryFilter:
    # The Platform API's query filters take a list even for a single value.
    return QueryFilter(field=field, operator="EQUALS", value=[value])


def in_filter(field: str, values: list[Any]) -> QueryFilter:
    return QueryFilter(field=field, operator="IN", value=list(values))


# --- entity fetch and naming -------------------------------------------------


def get_campaign(campaign_id: int):
    response = call("campaigns_id_get", id=str(campaign_id), x_ap_context=context_header())
    campaign = unwrap(response, "campaign")
    if campaign is None:
        raise ToolError(f"no campaign {campaign_id} in this ad account")
    return campaign


def get_ad_group(ad_group_id: int):
    response = call("adgroups_id_get", id=str(ad_group_id), x_ap_context=context_header())
    ad_group = unwrap(response, "ad group")
    if ad_group is None:
        raise ToolError(f"no ad group {ad_group_id} in this ad account")
    return ad_group


def get_keyword(keyword_id: int):
    response = call("keywords_id_get", id=str(keyword_id), x_ap_context=context_header())
    keyword = unwrap(response, "keyword")
    if keyword is None:
        raise ToolError(f"no keyword {keyword_id} in this ad account")
    return keyword


def get_negative_keyword(negative_keyword_id: int):
    response = call(
        "negative_keywords_id_get",
        id=str(negative_keyword_id),
        x_ap_context=context_header(),
    )
    negative = unwrap(response, "negative keyword")
    if negative is None:
        raise ToolError(f"no negative keyword {negative_keyword_id} in this ad account")
    return negative


def entity_path(
    campaign: Any = None,
    ad_group: Any = None,
    keyword_text: str | None = None,
    keyword_label: str = "Keyword",
) -> str:
    """`Campaign 'X' > AdGroup 'Y' > Keyword 'z'`.

    The single field that most changes whether an approval is a judgement or a
    reflex: ids alone are not reviewable, and a human asked to approve
    `keyword 1234567890` has no way to tell a brand term from a competitor one.
    """
    parts = []
    if campaign is not None:
        parts.append(f"Campaign {getattr(campaign, 'name', None)!r}")
    if ad_group is not None:
        parts.append(f"AdGroup {getattr(ad_group, 'name', None)!r}")
    if keyword_text is not None:
        parts.append(f"{keyword_label} {keyword_text!r}")
    return " > ".join(parts) if parts else "-"


def guarded_fetch(fetcher: Callable[[], T]) -> T:
    """Run a fetch, mapping anything unexpected onto ToolError."""
    try:
        return fetcher()
    except ToolError:
        raise
    except Exception as exc:
        raise ToolError(f"{type(exc).__name__}: {exc}") from exc
