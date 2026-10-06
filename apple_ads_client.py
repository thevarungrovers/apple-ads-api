#!/usr/bin/env python3
"""Thin wrapper over the Apple Search Ads Campaign Management API v5.

Every call carries two headers:
  Authorization: Bearer <access_token>
  X-AP-Context:  orgId=<orgId>

X-AP-Context is not optional -- a valid token without it still fails, because
one set of credentials can reach several orgs and the API will not guess which
one you mean. The exception is GET /acls, which is the endpoint that *tells*
you which orgs you can reach; it is called without the context header on
purpose, so a wrong orgId cannot mask a credential problem.

The client is READ-ONLY unless it is built with allow_writes=True. Apple runs
no sandbox for campaign management -- there is no test org, no staging account,
nothing to point this at that is not the live advertising account. A mutating
call therefore has to be asked for twice: once by opening the client for
writes, and again (for anything touching a serving campaign) by the caller.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import re
import sys

from _bootstrap import ensure_venv

ensure_venv()  # re-exec under venv/ if launched with a bare `python3`

import requests

from generate_client_secret import ConfigError, load_config
from get_token import TokenError, get_access_token


class AppleAdsError(RuntimeError):
    def __init__(self, status_code: int, url: str, body):
        self.status_code = status_code
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status_code} from {url}: {_short(body)}")


class WriteBlocked(RuntimeError):
    """A mutating call was made on a client that was not opened for writes.

    This is raised BEFORE the request leaves the process, so nothing reached
    Apple. It is deliberately not an AppleAdsError: callers that catch API
    failures should not accidentally swallow a refused write.
    """

    def __init__(self, method: str, url: str, payload):
        self.method = method
        self.url = url
        self.payload = payload
        preview = "" if payload is None else f"\n  payload: {_short(payload, 400)}"
        super().__init__(
            f"refused to send {method} {url} -- client is read-only.{preview}\n"
            "  Build the client with allow_writes=True, or pass --apply on the CLI."
        )


def _short(body, limit: int = 600) -> str:
    text = body if isinstance(body, str) else json.dumps(body)
    return text if len(text) <= limit else text[:limit] + "..."


class AppleAdsClient:
    BASE_URL = "https://api.searchads.apple.com/api/v5"

    # Apple uses POST for two endpoints that only READ: reporting, and the
    # /find selectors. Everything else posted creates something, so the two
    # shapes have to be told apart before a POST can be called safe.
    READ_ONLY_POST_PREFIXES = ("reports/",)
    READ_ONLY_POST_SUFFIXES = ("/find",)

    def __init__(
        self,
        org_id: str | None = None,
        config: dict | None = None,
        timeout: int = 60,
        allow_writes: bool = False,
    ):
        self.config = config or load_config()
        self.org_id = str(org_id or self.config["APPLE_ADS_ORG_ID"])
        self.timeout = timeout
        self.allow_writes = allow_writes
        self.session = requests.Session()

    def _headers(self, with_context: bool, force_refresh: bool = False) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {get_access_token(force_refresh=force_refresh, config=self.config)}",
            "Content-Type": "application/json",
        }
        if with_context:
            headers["X-AP-Context"] = f"orgId={self.org_id}"
        return headers

    @classmethod
    def is_mutation(cls, method: str, path: str) -> bool:
        """True when this call would change something in the ad account."""
        method = method.upper()
        if method in ("PUT", "PATCH", "DELETE"):
            return True
        if method != "POST":
            return False
        leaf = path.strip("/").lower()
        if leaf.startswith(cls.READ_ONLY_POST_PREFIXES):
            return False
        return not leaf.endswith(cls.READ_ONLY_POST_SUFFIXES)

    def request(self, method: str, path: str, *, with_context: bool = True, **kwargs):
        url = f"{self.BASE_URL}/{path.lstrip('/')}"

        if self.is_mutation(method, path) and not self.allow_writes:
            raise WriteBlocked(method.upper(), url, kwargs.get("json"))

        response = self.session.request(
            method, url, headers=self._headers(with_context), timeout=self.timeout, **kwargs
        )

        # A 401 on a cached token means it was revoked or rotated server-side.
        # Force one refresh and retry exactly once -- never loop, or a genuinely
        # bad credential turns into an infinite hammer on Apple's endpoint.
        if response.status_code == 401:
            response = self.session.request(
                method,
                url,
                headers=self._headers(with_context, force_refresh=True),
                timeout=self.timeout,
                **kwargs,
            )

        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise AppleAdsError(response.status_code, url, body)

        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError:
            return {"raw": response.text}

    def get(self, path: str, params: dict | None = None, **kwargs):
        return self.request("GET", path, params=params, **kwargs)

    def post(self, path: str, payload: dict | None = None, **kwargs):
        return self.request("POST", path, json=payload, **kwargs)

    def put(self, path: str, payload: dict | None = None, **kwargs):
        return self.request("PUT", path, json=payload, **kwargs)

    def delete(self, path: str, **kwargs):
        return self.request("DELETE", path, **kwargs)

    # --- convenience endpoints -------------------------------------------------

    def acls(self):
        """GET /acls -- the orgs these credentials can reach. No context header."""
        return self.get("acls", with_context=False)

    def campaigns(self, limit: int = 1000, offset: int = 0):
        """GET /campaigns -- every campaign in this org."""
        return self.get("campaigns", params={"limit": limit, "offset": offset})

    def ad_groups(self, campaign_id: int | str, limit: int = 1000, offset: int = 0):
        """GET /campaigns/{id}/adgroups -- every ad group under one campaign."""
        return self.get(f"campaigns/{campaign_id}/adgroups", params={"limit": limit, "offset": offset})

    def targeting_keywords(
        self, campaign_id: int | str, ad_group_id: int | str, limit: int = 1000, offset: int = 0
    ):
        """GET /campaigns/{cid}/adgroups/{gid}/targetingkeywords -- bid-on keywords.

        Keywords are namespaced per ad group, so a keywordId can only be resolved
        by walking the ad groups; there is no org-wide "get keyword by id". An ad
        group running Search Match returns an EMPTY list -- its traffic carries no
        keywordId at all, which is why attribution rows legitimately have a NULL
        keyword_id rather than an unresolvable one.
        """
        return self.get(
            f"campaigns/{campaign_id}/adgroups/{ad_group_id}/targetingkeywords",
            params={"limit": limit, "offset": offset},
        )

    def campaign_report(
        self,
        start_date: str,
        end_date: str,
        granularity: str | None = "DAILY",
        limit: int = 1000,
        offset: int = 0,
    ):
        """POST /reports/campaigns -- performance metrics per campaign.

        This is the API equivalent of the Performance CSV export that marketing
        imports by hand today (spend, impressions, taps, installs, avg CPT).
        Apple caps a DAILY-granularity range at 90 days.
        """
        payload = {
            "startTime": start_date,
            "endTime": end_date,
            "selector": {
                "orderBy": [{"field": "campaignId", "sortOrder": "ASCENDING"}],
                "pagination": {"offset": offset, "limit": limit},
            },
            "returnRowTotals": True,
            "returnGrandTotals": True,
            # Must stay False when a granularity is set -- Apple rejects the
            # combination of granularity with returnRecordsWithNoMetrics=True.
            "returnRecordsWithNoMetrics": False,
        }
        if granularity:
            payload["granularity"] = granularity
        return self.post("reports/campaigns", payload)


def _parse_payload(raw: str | None):
    """--data as inline JSON, or @path to read it from a file."""
    if raw is None:
        return None
    try:
        text = pathlib.Path(raw[1:]).read_text() if raw.startswith("@") else raw
    except OSError as exc:
        raise ConfigError(f"could not read {raw[1:]}: {exc}") from None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise ConfigError(f"--data is not valid JSON: {exc}") from None


def _campaign_in_path(path: str) -> str | None:
    """The campaign id a mutating path points at, if it names an existing one.

    POST /campaigns creates a new one and matches nothing here, which is the
    point: there is no live campaign to protect yet.
    """
    match = re.match(r"campaigns/(\d+)", path.strip("/").lower())
    return match.group(1) if match else None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Call an Apple Search Ads API v5 endpoint and print the JSON response.",
        epilog="examples:\n"
               "  ./venv/bin/python apple_ads_client.py acls\n"
               "  ./venv/bin/python apple_ads_client.py campaigns\n"
               "  ./venv/bin/python apple_ads_client.py acls --raw\n"
               "\n"
               "writes are a dry run unless you add --apply:\n"
               "  ./venv/bin/python apple_ads_client.py campaigns -X POST -d @new-campaign.json\n"
               "  ./venv/bin/python apple_ads_client.py campaigns -X POST -d @new-campaign.json --apply\n"
               "  ./venv/bin/python apple_ads_client.py campaigns/123 -X PUT -d '{\"status\":\"PAUSED\"}' \\\n"
               "      --apply --confirm-live",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("path", help="endpoint path relative to /api/v5, e.g. acls or campaigns")
    parser.add_argument("-X", "--method", default="GET", help="HTTP method (default: %(default)s)")
    parser.add_argument("-d", "--data", default=None,
                        help="JSON request body, or @path to read it from a file")
    parser.add_argument("--apply", action="store_true",
                        help="actually send a mutating call. Without it, writes are a dry run")
    parser.add_argument("--confirm-live", action="store_true",
                        help="also allow the write when the campaign it targets is serving")
    parser.add_argument("--org-id", default=None, help="override APPLE_ADS_ORG_ID")
    parser.add_argument("--no-context", action="store_true", help="omit the X-AP-Context header")
    parser.add_argument("--raw", action="store_true", help="print the full response, unabridged")
    args = parser.parse_args()

    method = args.method.upper()
    mutating = AppleAdsClient.is_mutation(method, args.path)
    url = f"{AppleAdsClient.BASE_URL}/{args.path.lstrip('/')}"

    try:
        payload = _parse_payload(args.data)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if payload is not None and method == "GET":
        print("error: --data needs a method that carries a body, e.g. -X POST", file=sys.stderr)
        return 1

    # The dry run is the default for anything that would change the account.
    # It prints the exact request and stops, so the cost of being wrong about a
    # payload is a line of output rather than a change to live ads.
    if mutating and not args.apply:
        print("DRY RUN -- nothing was sent\n")
        print(f"  {method} {url}")
        if not args.no_context:
            print(f"  X-AP-Context: orgId={args.org_id or '<APPLE_ADS_ORG_ID>'}")
        if payload is not None:
            print(f"  payload: {json.dumps(payload, indent=2)}")
        print("\nAdd --apply to send it.")
        return 0

    try:
        client = AppleAdsClient(org_id=args.org_id, allow_writes=args.apply)

        # A write aimed at a campaign that is currently serving needs a second,
        # different flag. --apply alone means "I meant to write"; --confirm-live
        # means "I meant to write to something that is spending money today".
        if mutating:
            campaign_id = _campaign_in_path(args.path)
            if campaign_id:
                data = (client.get(f"campaigns/{campaign_id}") or {}).get("data") or {}
                if data.get("servingStatus") == "RUNNING" and not args.confirm_live:
                    print(
                        f"error: campaign {campaign_id} ({data.get('name')}) is serving right now.\n"
                        "  Re-run with --confirm-live if that is what you meant.",
                        file=sys.stderr,
                    )
                    return 1

        body = client.request(
            method, args.path, with_context=not args.no_context,
            **({"json": payload} if payload is not None else {}),
        )
    except WriteBlocked as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (ConfigError, TokenError, AppleAdsError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"error: network failure calling Apple: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(body, indent=2)
    print(text if args.raw or len(text) <= 4000 else text[:4000] + "\n... (truncated; use --raw)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
