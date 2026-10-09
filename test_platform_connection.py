#!/usr/bin/env python3
"""End-to-end check of the Apple Ads **Platform API** chain, one rung at a time.

The sibling of test_connection.py, which checks the v5 API. Same ladder, same
reasoning: each step is a narrower failure than the one after it, so the first
FAIL tells you where the problem actually is instead of leaving a single opaque
error.

  1. local files and .env are present and coherent
  2. the private key is really EC P-256
  3. the client secret signs, and is not near its 180-day expiry
  4. Apple issues an access token                -> credentials are valid
  5. GET /me/acls lists our ad accounts          -> ** THIS ANSWERS adAccountId **
  6. POST /campaigns/find returns data           -> X-AP-Context works
  7. POST /reports/campaigns returns metrics     -> real reporting data

Step 5 is the whole point of this script. The Platform API is addressed by
**adAccountId**, which is NOT the org id: Apple returns `id` and `orgId` as two
separate fields on the same ACL record, and confusing them writes to the wrong
account. The id cannot be guessed and should not be read off the UI, so step 5
reads it back from Apple and prints the exact line to paste into .env.

Auth is unchanged from v5 -- same ES256 JWT, same token endpoint, same
`private-key.pem` and the same three .env credentials. What changes is the host
(api.ads.apple.com/v1 rather than api.searchads.apple.com/api/v5) and the
context header (`adAccountId=<id>;`, with the trailing semicolon, rather than
`orgId=<id>`).

  ./venv/bin/python test_platform_connection.py
  ./venv/bin/python test_platform_connection.py --days 7
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys

from _bootstrap import ensure_venv

ensure_venv()  # re-exec under venv/ if launched with a bare `python3`

from apple_ads_platform import (
    ApiException,
    AppsOptions,
    AppsReportingRequest,
    QueryPagination,
    QueryRequest,
    TimeRange,
)
from apple_ads_platform.auth.client_secret import EphemeralClientSecretProvider
from apple_ads_platform.auth.errors import AuthError
from apple_ads_platform.builder import AppleAdsClientBuilder
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from generate_client_secret import (
    AD_ACCOUNT_KEY,
    ENV_FILE,
    PRIVATE_KEY,
    REQUIRED_KEYS,
    ConfigError,
    load_config,
    mask,
)

REPORT_WINDOW_DAYS = 7
EXPIRY_WARNING_DAYS = 14
API_TIMEOUT = 30.0  # the 5.0s default is too short for a report call

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def step(number: int, title: str) -> None:
    print(f"\n{DIM}[{number}/7]{RESET} {title}")


def ok(message: str) -> None:
    print(f"  {GREEN}PASS{RESET}  {message}")


def warn(message: str) -> None:
    print(f"  {YELLOW}WARN{RESET}  {message}")


def fail(message: str) -> None:
    print(f"  {RED}FAIL{RESET}  {message}")


def context_header(ad_account_id: str | int) -> str:
    """`adAccountId=<id>;` -- the trailing semicolon is Apple's format, not a typo."""
    return f"adAccountId={ad_account_id};"


def _enum(value) -> str:
    """`CampaignStatus.ENABLED` reads badly in a table; the wire value does not."""
    return getattr(value, "value", value) if value is not None else "-"


def _money(value) -> str:
    if value is None:
        return "-"
    return f"{getattr(value, 'amount', '-')} {getattr(value, 'currency', '') or ''}".strip()


def run(window_days: int) -> int:
    # 1 -- local files ---------------------------------------------------------
    step(1, "Local files and configuration")
    for path in (PRIVATE_KEY, ENV_FILE):
        if not path.exists():
            fail(f"{path.name} is missing")
            if path is ENV_FILE:
                print("\n  Copy .env.example to .env and fill in the values from")
                print("  ads.apple.com -> Account Settings -> API.")
            return 1
    ok(f"{PRIVATE_KEY.name} and {ENV_FILE.name} present")

    mode = oct(PRIVATE_KEY.stat().st_mode & 0o777)
    (ok if mode == "0o600" else warn)(
        f"{PRIVATE_KEY.name} mode is {mode}" + ("" if mode == "0o600" else " (expected 0o600)")
    )

    try:
        config = load_config()
    except ConfigError as exc:
        fail(str(exc))
        return 1
    for key in REQUIRED_KEYS:
        ok(f"{key} = {mask(config[key])}")

    # Absent is the EXPECTED state the first time this runs -- step 5 is what
    # fills it in. A value that is already there gets cross-checked in step 5.
    cached_account_id = (config.get(AD_ACCOUNT_KEY) or "").strip()
    if cached_account_id:
        ok(f"{AD_ACCOUNT_KEY} = {cached_account_id} (will be cross-checked in step 5)")
    else:
        warn(f"{AD_ACCOUNT_KEY} is not set yet -- step 5 discovers it")

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
    # Signed with the library's own provider rather than generate_client_secret,
    # so what is checked here is the code path the Platform client will actually
    # use. The provider re-derives the JWT on every token fetch, which is why a
    # long-lived server never has to rebuild its client to dodge an expiry.
    step(3, "Sign the client secret (ES256, via the Platform SDK's provider)")
    try:
        provider = EphemeralClientSecretProvider(
            client_id=config["APPLE_ADS_CLIENT_ID"],
            team_id=config["APPLE_ADS_TEAM_ID"],
            key_id=config["APPLE_ADS_KEY_ID"],
            private_key_pem=PRIVATE_KEY.read_text(),
        )
        secret = provider.create_secret()
    except AuthError as exc:
        fail(str(exc))
        return 1

    import jwt  # already a dependency of both clients

    payload = jwt.decode(secret, options={"verify_signature": False})
    expires_at = dt.datetime.fromtimestamp(payload["exp"], dt.timezone.utc)
    days_left = (expires_at - dt.datetime.now(dt.timezone.utc)).days
    ok(f"signed; sub={mask(payload['sub'])} iss={mask(payload['iss'])} aud={payload['aud']}")
    message = f"expires {expires_at:%Y-%m-%d} ({days_left} days)"
    (warn if days_left <= EXPIRY_WARNING_DAYS else ok)(message)

    # 4 -- access token --------------------------------------------------------
    # build() constructs a TokenManager, which fetches a token eagerly. So this
    # step really does exercise the token exchange, not just object construction.
    step(4, "Exchange it for an access token (builder.build())")
    try:
        api = (
            AppleAdsClientBuilder.from_private_key(
                client_id=config["APPLE_ADS_CLIENT_ID"],
                team_id=config["APPLE_ADS_TEAM_ID"],
                key_id=config["APPLE_ADS_KEY_ID"],
                private_key=PRIVATE_KEY.read_text(),
            )
            .api_timeout(API_TIMEOUT)
            .build()
        )
    except AuthError as exc:
        fail(f"token exchange failed: {exc}")
        return 1
    except Exception as exc:  # network, TLS, proxy...
        fail(f"could not reach appleid.apple.com: {exc}")
        return 1
    ok("access token acquired; client built against https://api.ads.apple.com/v1")

    # 5 -- ACL: THE adAccountId QUESTION ---------------------------------------
    # The only call in the whole API that takes no context header, which is what
    # makes it the right place to start: it cannot fail for header reasons.
    step(5, "GET /me/acls  (no context header -- this is where adAccountId comes from)")
    try:
        acl_response = api.get_user_acls()
    except ApiException as exc:
        fail(f"HTTP {exc.status}: {exc.reason} {getattr(exc, 'body', '')}")
        return 1
    except Exception as exc:
        fail(f"network failure reaching api.ads.apple.com: {exc}")
        return 1

    if acl_response.error is not None:
        fail(f"API returned an error: {acl_response.error}")
        return 1

    acls = (acl_response.result.acls if acl_response.result else None) or []
    if not acls:
        fail("no ad accounts returned -- these credentials cannot reach any account")
        return 1

    ok(f"{len(acls)} ad account(s) reachable:")
    print(f"        {'adAccountId':<14} {'orgId':<12} {'name':<34} roles")
    for acl in acls:
        account = acl.ad_account
        roles = ", ".join(acl.roles or []) or "?"
        print(
            f"        {str(getattr(account, 'id', '?')):<14} "
            f"{str(getattr(account, 'org_id', '?')):<12} "
            f"{(getattr(account, 'name', '') or '')[:34]:<34} {roles}"
        )

    # One org can own SEVERAL ad accounts -- verified live 2026-10-08, where two
    # accounts share a single orgId. So orgId does NOT identify an ad account,
    # and matching on it can only ever narrow the field, never decide. Where it
    # does not decide, this script stops and makes a human choose: picking the
    # first row would be a silent coin-flip over which account gets written to.
    org_id = str(config["APPLE_ADS_ORG_ID"])
    by_org = [a for a in acls if str(getattr(a.ad_account, "org_id", "")) == org_id]

    if cached_account_id:
        match = [a for a in acls if str(getattr(a.ad_account, "id", "")) == cached_account_id]
        if not match:
            fail(
                f"{AD_ACCOUNT_KEY} in .env is {cached_account_id}, which is not an "
                f"account these credentials can reach. Pick one from the table above."
            )
            return 1
        resolved = match[0].ad_account
        ok(f"{AD_ACCOUNT_KEY} {cached_account_id} confirmed against the ACL "
           f"({resolved.name})")
    elif len(by_org) == 1:
        resolved = by_org[0].ad_account
        ok(f"exactly one account under APPLE_ADS_ORG_ID {org_id} -> adAccountId {resolved.id}")
    elif len(acls) == 1:
        resolved = acls[0].ad_account
        warn(f"no account has orgId {org_id}; only one is reachable, using it")
    else:
        fail(
            f"{len(by_org) or len(acls)} ad accounts to choose from and nothing in .env "
            f"says which. Copy the right adAccountId out of the table above into "
            f"{AD_ACCOUNT_KEY} and re-run -- note that orgId does not disambiguate "
            f"them, and guessing is how you write to the wrong account."
        )
        return 1

    resolved_id = str(resolved.id)
    if resolved_id == org_id:
        warn(f"adAccountId {resolved_id} happens to EQUAL the orgId here. That is a "
             f"coincidence of this account, not a rule -- do not start treating them "
             f"as the same field.")
    if not cached_account_id:
        print()
        print(f"  {YELLOW}Add this line to .env:{RESET}")
        print(f"      {AD_ACCOUNT_KEY}={resolved_id}")

    ctx = context_header(resolved_id)

    # 6 -- campaigns -----------------------------------------------------------
    # A wrong header format fails right here, cheaply, with a 401/403 -- before
    # any report call has had a chance to burn 30 seconds on the same mistake.
    step(6, f"POST /campaigns/find  (X-AP-Context: {ctx})")
    try:
        campaigns_response = api.campaigns_query_post(
            x_ap_context=ctx,
            query_request=QueryRequest(
                pagination=QueryPagination(page_size=20, fetch_total_count=True)
            ),
        )
    except ApiException as exc:
        fail(f"HTTP {exc.status}: {exc.reason} {getattr(exc, 'body', '')}")
        if exc.status in (401, 403):
            print(f"        {DIM}401/403 here usually means the context header format "
                  f"is wrong, not that the token is bad -- step 5 passed.{RESET}")
        return 1
    except Exception as exc:
        fail(f"network failure: {exc}")
        return 1

    if campaigns_response.error is not None:
        fail(f"API returned an error: {campaigns_response.error}")
        return 1

    campaigns = campaigns_response.result or []
    total = getattr(campaigns_response.pagination, "total_count", None)
    ok(f"{total if total is not None else len(campaigns)} campaign(s); showing {len(campaigns)}:")
    for campaign in campaigns:
        budget = campaign.daily_budget.value if campaign.daily_budget else None
        print(
            f"        {campaign.id}  {campaign.name}  "
            f"[{_enum(campaign.status)}/{_enum(campaign.display_status)}] "
            f"daily budget: {_money(budget)}"
        )
    if not campaigns:
        warn("no campaigns returned -- the chain works, the account just has none")

    # 7 -- reporting data ------------------------------------------------------
    end = dt.date.today()
    start = end - dt.timedelta(days=window_days - 1)
    step(7, f"POST /reports/campaigns  ({start} to {end}, DAILY)")
    try:
        report = api.apps_campaign_reports(
            x_ap_context=ctx,
            apps_reporting_request=AppsReportingRequest(
                time_range=TimeRange(start=start, end=end, granularity="DAILY"),
                options=AppsOptions(include_rows=["GRAND_TOTAL"]),
            ),
            _request_timeout=60.0,
        )
    except ApiException as exc:
        fail(f"HTTP {exc.status}: {exc.reason} {getattr(exc, 'body', '')}")
        return 1
    except Exception as exc:
        fail(f"network failure: {exc}")
        return 1

    if report.error is not None:
        fail(f"API returned an error: {report.error}")
        return 1

    rows = (report.result.rows if report.result else None) or []
    ok(f"{len(rows)} campaign row(s) returned")

    summary = report.result.summary if report.result else None
    grand_total = getattr(summary, "grand_total", None)
    if grand_total is not None:
        print(
            f"        grand totals: spend {_money(grand_total.local_spend)} | "
            f"impressions {grand_total.impressions} | taps {grand_total.taps} | "
            f"installs {grand_total.total_installs} "
            f"(new {grand_total.total_new_downloads}, redl {grand_total.total_redownloads})"
        )
    elif not rows:
        warn(f"no delivery in the last {window_days} days -- an empty report is a valid response")

    print(f"\n{GREEN}All checks passed.{RESET} The Platform API chain works: "
          "key -> client secret -> access token -> adAccountId -> API.")
    if not cached_account_id:
        print(f"{DIM}Next: add {AD_ACCOUNT_KEY}={resolved_id} to .env, then re-run this "
              f"script -- step 1 will confirm it.{RESET}")
    else:
        print(f"{DIM}Next: start the MCP server, or compare step 7 against "
              f"`fetch_campaign_report.py --days {window_days}` (v5).{RESET}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check the Apple Ads Platform API chain and resolve adAccountId.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--days",
        type=int,
        default=REPORT_WINDOW_DAYS,
        help=f"reporting window for step 7 (default: %(default)s)",
    )
    args = parser.parse_args()
    return run(max(1, args.days))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
