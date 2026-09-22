#!/usr/bin/env python3
"""End-to-end check of the Apple Search Ads chain, one rung at a time.

Each step is a narrower failure than the one after it, so the first FAIL tells
you where the problem actually is instead of leaving a single opaque error:

  1. local files and .env are present and coherent
  2. the private key is really EC P-256
  3. the client secret signs, and is not near its 180-day expiry
  4. Apple issues an access token             -> credentials are valid
  5. GET /acls lists our org                  -> this org is reachable
  6. GET /campaigns returns data              -> X-AP-Context works
  7. POST /reports/campaigns returns metrics  -> real reporting data
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys

from _bootstrap import ensure_venv

ensure_venv()  # re-exec under venv/ if launched with a bare `python3`

import requests
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from generate_client_secret import (
    ENV_FILE,
    PRIVATE_KEY,
    REQUIRED_KEYS,
    ConfigError,
    build_client_secret,
    load_config,
    mask,
)
from apple_ads_client import AppleAdsClient, AppleAdsError
from get_token import TokenError, get_access_token, token_status

REPORT_WINDOW_DAYS = 7
EXPIRY_WARNING_DAYS = 14

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def step(number: int, title: str) -> None:
    print(f"\n{DIM}[{number}/7]{RESET} {title}")


def ok(message: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {message}")


def warn(message: str) -> None:
    print(f"  {YELLOW}WARN{RESET}  {message}")


def fail(message: str) -> None:
    print(f"  {RED}FAIL{RESET}  {message}")


def run() -> int:
    org_id = None

    # 1 -- local files ---------------------------------------------------------
    step(1, "Local files and configuration")
    for path in (PRIVATE_KEY, ENV_FILE):
        if not path.exists():
            fail(f"{path.name} is missing")
            if path is ENV_FILE:
                print(f"\n  Copy .env.example to .env and fill in the three values from")
                print("  ads.apple.com -> Account Settings -> API.")
            return 1
    ok(f"{PRIVATE_KEY.name} and {ENV_FILE.name} present")

    mode = oct(PRIVATE_KEY.stat().st_mode & 0o777)
    (ok if mode == "0o600" else warn)(f"{PRIVATE_KEY.name} mode is {mode}" + ("" if mode == "0o600" else " (expected 0o600)"))

    try:
        config = load_config()
    except ConfigError as exc:
        fail(str(exc))
        return 1
    for key in REQUIRED_KEYS:
        ok(f"{key} = {mask(config[key])}")
    org_id = config["APPLE_ADS_ORG_ID"]
    ok(f"APPLE_ADS_ORG_ID = {org_id}")

    # 2 -- key material --------------------------------------------------------
    step(2, "Private key is EC P-256")
    try:
        private_key = load_pem_private_key(PRIVATE_KEY.read_bytes(), password=None)
        curve = getattr(private_key, "curve", None)
        if curve is None or curve.name != "secp256r1":
            fail(f"key is not P-256 (curve: {getattr(curve, 'name', type(private_key).__name__)})")
            return 1
        ok(f"{curve.name} / P-256, {curve.key_size}-bit")
    except Exception as exc:
        fail(f"could not read {PRIVATE_KEY.name}: {exc}")
        return 1

    # 3 -- client secret -------------------------------------------------------
    step(3, "Sign the client secret (ES256)")
    try:
        _, payload = build_client_secret(config=config)
    except ConfigError as exc:
        fail(str(exc))
        return 1
    expires_at = dt.datetime.fromtimestamp(payload["exp"], dt.timezone.utc)
    days_left = (expires_at - dt.datetime.now(dt.timezone.utc)).days
    ok(f"signed; sub={mask(payload['sub'])} iss={mask(payload['iss'])} aud={payload['aud']}")
    message = f"expires {expires_at:%Y-%m-%d} ({days_left} days)"
    (warn if days_left <= EXPIRY_WARNING_DAYS else ok)(message)

    # 4 -- access token --------------------------------------------------------
    step(4, "Exchange it for an access token")
    try:
        get_access_token()
    except (ConfigError, TokenError) as exc:
        fail(str(exc))
        return 1
    except requests.RequestException as exc:
        fail(f"network failure reaching appleid.apple.com: {exc}")
        return 1
    status = token_status()
    ok(f"token acquired, {status['seconds_remaining']}s remaining, scope={status['scope']}")

    client = AppleAdsClient(config=config)

    # 5 -- ACL -----------------------------------------------------------------
    step(5, "GET /api/v5/acls")
    try:
        acls = client.acls()
    except (AppleAdsError, requests.RequestException) as exc:
        fail(str(exc))
        return 1

    orgs = acls.get("data") or []
    if not orgs:
        fail(f"no orgs returned: {acls}")
        return 1
    ok(f"{len(orgs)} org(s) reachable:")
    for org in orgs:
        marker = " <-- target" if str(org.get("orgId")) == str(org_id) else ""
        roles = ", ".join(org.get("roleNames") or []) or "?"
        print(f"        {org.get('orgId')}  {org.get('orgName')}  "
              f"[{org.get('currency')}, {org.get('timeZone')}] roles: {roles}{marker}")

    if not any(str(org.get("orgId")) == str(org_id) for org in orgs):
        fail(f"orgId {org_id} is NOT in the ACL -- these credentials cannot reach it")
        return 1
    ok(f"orgId {org_id} confirmed in the ACL")

    # 6 -- campaigns -----------------------------------------------------------
    step(6, f"GET /api/v5/campaigns  (X-AP-Context: orgId={org_id})")
    try:
        campaigns = client.campaigns(limit=20)
    except (AppleAdsError, requests.RequestException) as exc:
        fail(str(exc))
        return 1

    rows = campaigns.get("data") or []
    total = (campaigns.get("pagination") or {}).get("totalResults", len(rows))
    ok(f"{total} campaign(s) in this org; showing up to {len(rows)}:")
    for campaign in rows:
        budget = (campaign.get("dailyBudgetAmount") or {})
        print(f"        {campaign.get('id')}  {campaign.get('name')}  "
              f"[{campaign.get('status')}/{campaign.get('servingStatus')}] "
              f"daily budget: {budget.get('amount', '-')} {budget.get('currency', '')}")
    if not rows:
        warn("no campaigns returned -- the chain works, the org just has none")

    # 7 -- reporting data ------------------------------------------------------
    end = dt.date.today()
    start = end - dt.timedelta(days=REPORT_WINDOW_DAYS - 1)
    step(7, f"POST /api/v5/reports/campaigns  ({start} to {end}, DAILY)")
    try:
        report = client.campaign_report(start.isoformat(), end.isoformat())
    except (AppleAdsError, requests.RequestException) as exc:
        fail(str(exc))
        return 1

    response = (report.get("data") or {}).get("reportingDataResponse") or {}
    report_rows = response.get("row") or []
    ok(f"{len(report_rows)} campaign row(s) returned")

    totals = (response.get("grandTotals") or {}).get("total") or {}
    if totals:
        spend = totals.get("localSpend") or {}
        # v5 names these totalInstalls / totalNewDownloads / totalRedownloads.
        # The bare `installs` key does not exist and read as '-' here.
        print(f"        grand totals: spend {spend.get('amount', '-')} {spend.get('currency', '')} | "
              f"impressions {totals.get('impressions', '-')} | taps {totals.get('taps', '-')} | "
              f"installs {totals.get('totalInstalls', '-')} "
              f"(new {totals.get('totalNewDownloads', '-')}, "
              f"redl {totals.get('totalRedownloads', '-')})")
    elif not report_rows:
        warn(f"no delivery in the last {REPORT_WINDOW_DAYS} days -- an empty report is a valid response")

    print(f"\n{GREEN}All checks passed.{RESET} The full chain works: "
          "key -> client secret -> access token -> API.")
    print(f"{DIM}Next: python3 fetch_campaign_report.py --days 30{RESET}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
