#!/usr/bin/env python3
"""Reads retry on 429/5xx/timeouts; writes never do; a rebuild closes the old token timer.

Run it without touching Apple:
    ./venv/bin/python tests/test_retry.py
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import REAL_CALL, no_live_client, run, temp_db  # noqa: E402

import urllib3  # noqa: E402
from apple_ads_platform import ApiException  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from apple_ads_mcp import client  # noqa: E402


class _Api:
    """Fails `failures` times with `error`, then answers."""

    def __init__(self, error, failures: int) -> None:
        self.error, self.failures, self.calls = error, failures, 0

    def __getattr__(self, name):
        def method(**_kwargs):
            self.calls += 1
            if self.calls <= self.failures:
                raise self.error
            return "ok"

        return method


def _with(api) -> list[float]:
    no_live_client()
    temp_db()
    client.call = REAL_CALL  # another test module may have installed a fake
    client._api = api
    slept: list[float] = []
    client._sleep = slept.append
    return slept


def test_a_read_retries_a_429_and_honours_retry_after() -> None:
    error = ApiException(status=429, reason="Too Many Requests")
    error.headers = {"Retry-After": "7"}
    api = _Api(error, failures=2)
    slept = _with(api)
    assert client.call("campaigns_query_post") == "ok"
    assert api.calls == 3 and slept == [7.0, 7.0], (api.calls, slept)


def test_a_read_retries_a_5xx_and_a_timeout_with_backoff() -> None:
    for error in (ApiException(status=503, reason="down"),
                  urllib3.exceptions.ReadTimeoutError(None, "/v1", "timed out")):
        api = _Api(error, failures=1)
        slept = _with(api)
        assert client.call("apps_keyword_reports_without_preload_content") == "ok"
        assert api.calls == 2 and slept == [1.0], (error, api.calls, slept)


def test_a_read_gives_up_after_three_attempts() -> None:
    api = _Api(ApiException(status=500, reason="err"), failures=10)
    _with(api)
    try:
        client.call("keywords_id_get")
    except ToolError as exc:
        assert str(exc).startswith("TRANSIENT: "), exc
    else:
        raise AssertionError("expected a ToolError")
    assert api.calls == 3


def test_a_write_is_never_retried() -> None:
    for method in ("keywords_id_put", "negative_keywords_bulk_create_post", "campaigns_post",
                   "dismiss_daily_budget_recommendations"):
        api = _Api(ApiException(status=503, reason="down"), failures=1)
        slept = _with(api)
        try:
            client.call(method)
        except ToolError:
            pass
        assert api.calls == 1 and slept == [], (method, api.calls)


def test_a_400_is_not_retried() -> None:
    api = _Api(ApiException(status=400, reason="bad"), failures=1)
    _with(api)
    try:
        client.call("campaigns_query_post")
    except ToolError as exc:
        assert str(exc).startswith("INVALID: ")
    assert api.calls == 1


def test_reset_closes_the_old_token_manager() -> None:
    closed = []

    class Provider:
        def close(self):
            closed.append(True)

    class Api:
        api_client = type("C", (), {"_access_token_provider": Provider()})()

    client._api = Api()
    client.reset_api()
    assert closed == [True] and client._api is None


def test_read_classification() -> None:
    reads = ["campaigns_query_post", "keywords_id_get", "apps_campaign_reports",
             "apps_ad_reports_without_preload_content", "query_keyword_suggestions",
             "search_geos", "get_geos_by_ids", "impression_share_query", "get_me"]
    writes = ["keywords_id_put", "campaigns_post", "keywords_bulk_create_post",
              "dismiss_target_cpa_recommendations", "create_location_group",
              "negative_keywords_bulk_update_post"]
    assert all(client.is_read(m) for m in reads), [m for m in reads if not client.is_read(m)]
    assert not any(client.is_read(m) for m in writes), [m for m in writes if client.is_read(m)]


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
