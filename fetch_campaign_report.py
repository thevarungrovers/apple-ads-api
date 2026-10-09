#!/usr/bin/env python3
"""Pull daily campaign performance from the Apple Ads Platform API and print/save it.

Column names mirror the Performance CSV that marketing exports by hand, so this
output reconciles against apple_ads_daily row for row:

    Installs (Total)      -> totalInstalls
    New Downloads (Total) -> totalNewDownloads
    Redownloads (Total)   -> totalRedownloads

Install metrics are named `totalInstalls` / `totalNewDownloads` /
`totalRedownloads`. The bare `installs` / `newDownloads` / `redownloads` keys the
v3 API used do not exist, and Apple returns absent keys silently rather than
erroring, so a wrong name reads as an empty column rather than as a failure.

Ported from v5 to the Platform API on 2026-10-09 and diffed against the v5
output for the same window before v5 was removed -- the two agreed row for row.

  ./venv/bin/python fetch_campaign_report.py                    # last 7 days
  ./venv/bin/python fetch_campaign_report.py --days 30
  ./venv/bin/python fetch_campaign_report.py --start 2026-08-01 --end 2026-08-28
  ./venv/bin/python fetch_campaign_report.py --days 30 --csv
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import pathlib
import sys

from _bootstrap import ensure_venv

ensure_venv()  # re-exec under venv/ if launched with a bare `python3`

from apple_ads_platform import AppsOptions, AppsReportingRequest, TimeRange

from apple_ads_mcp.client import REPORT_TIMEOUT, call, context_header
from apple_ads_mcp.config import ConfigError, resolve_ad_account_id

HERE = pathlib.Path(__file__).resolve().parent
REPORTS_DIR = HERE / "reports"

# Apple caps a DAILY-granularity report at 90 days per request.
MAX_DAILY_RANGE = 90

COLUMNS = [
    "date", "campaign_id", "campaign_name", "currency",
    "spend", "impressions", "taps", "ttr", "avg_cpt",
    "installs_total", "new_downloads_total", "redownloads_total",
    "tap_installs", "view_installs", "avg_cpi_total", "avg_cpm", "install_rate_total",
]


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected YYYY-MM-DD, got {value!r}")


def amount(bucket: dict, key: str) -> str:
    """Apple wraps money as {"amount": "4.4533", "currency": "CAD"}."""
    return (bucket.get(key) or {}).get("amount", "")


def fetch(start: dt.date, end: dt.date, granularity: str) -> dict:
    """POST /reports/apps/campaigns, returned as raw JSON.

    Read through `*_without_preload_content` rather than the SDK's response
    models on purpose: apple-ads-platform 1.109.0 generates some reporting enums
    that the live API contradicts, and its validators raise instead of falling
    back, so a correct response can fail to deserialize client-side. This script
    flattens the payload anyway, so the models buy nothing.
    """
    response = call(
        "apps_campaign_reports_without_preload_content",
        x_ap_context=context_header(),
        apps_reporting_request=AppsReportingRequest(
            time_range=TimeRange(start=start, end=end, granularity=granularity),
            options=AppsOptions(include_rows=["GRAND_TOTAL"]),
        ),
        _request_timeout=REPORT_TIMEOUT,
    )
    payload = json.loads(response.data.decode("utf-8"))
    if payload.get("error"):
        raise ConfigError(f"Apple rejected the report request: {payload['error']}")
    return payload


def _report_currency(report: dict) -> str:
    """The first currency anywhere in the response.

    Needed because on a ZERO-DELIVERY day the Platform API omits `localSpend`
    entirely, where v5 returned {"amount": "0", "currency": "CAD"}. Reading the
    absent key would blank both columns and make a quiet day look like missing
    data rather than a day with no spend.
    """
    for row in (report.get("result") or {}).get("rows") or []:
        for bucket in (row.get("granularMetrics") or []) + [row.get("totalMetrics")]:
            currency = ((bucket or {}).get("localSpend") or {}).get("currency")
            if currency:
                return currency
    return ""


def flatten(report: dict) -> list[dict]:
    """Turn Apple's nested row/granularity response into flat daily records.

    Platform API names differ from v5's in four places and are otherwise
    identical: `granularMetrics` (was `granularity`), `totalMetrics` (was
    `total`), `cpt` (was `avgCPT`) and `cpm` (was `avgCPM`). The campaign id and
    name also move from metadata.campaignId/campaignName to metadata.id/name.
    """
    records = []
    fallback_currency = _report_currency(report)

    for row in (report.get("result") or {}).get("rows") or []:
        metadata = row.get("metadata") or {}
        # With a granularity set, metrics live under `granularMetrics`; without
        # one a single aggregate sits under `totalMetrics` instead.
        buckets = row.get("granularMetrics") or (
            [row.get("totalMetrics")] if row.get("totalMetrics") else []
        )

        for bucket in buckets:
            if not bucket:
                continue
            spend = bucket.get("localSpend") or {}
            records.append({
                "date": bucket.get("date", ""),
                "campaign_id": metadata.get("id", ""),
                "campaign_name": metadata.get("name", ""),
                # A day with no delivery carries no localSpend at all, so these
                # two are defaulted rather than read as blank -- "0 CAD spent"
                # and "no data" are different claims.
                "currency": spend.get("currency") or fallback_currency,
                "spend": spend.get("amount", "0"),
                "impressions": bucket.get("impressions", 0),
                "taps": bucket.get("taps", 0),
                # ttr and the install rates are raw RATIOS here (0.0042), not the
                # percentages the CSV export shows. Do not multiply by 100 before
                # comparing the two, and do not sum them -- re-derive from totals.
                "ttr": bucket.get("ttr", ""),
                "avg_cpt": amount(bucket, "cpt"),
                "installs_total": bucket.get("totalInstalls", 0),
                "new_downloads_total": bucket.get("totalNewDownloads", 0),
                "redownloads_total": bucket.get("totalRedownloads", 0),
                # tap- vs view-attributed installs. The CSV export does not break
                # these out; total = tap + view.
                "tap_installs": bucket.get("tapInstalls", 0),
                "view_installs": bucket.get("viewInstalls", 0),
                "avg_cpi_total": amount(bucket, "totalAvgCPI"),
                "avg_cpm": amount(bucket, "cpm"),
                "install_rate_total": bucket.get("totalInstallRate", ""),
            })

    records.sort(key=lambda record: (record["date"], record["campaign_name"]))
    return records


def print_table(records: list[dict]) -> None:
    header = (f"{'DATE':<12}{'CAMPAIGN':<40}{'SPEND':>10}{'IMPR':>8}{'TAPS':>6}"
              f"{'INST':>6}{'NEW':>6}{'REDL':>6}{'AVG CPT':>9}")
    print(header)
    print("-" * len(header))
    for record in records:
        name = record["campaign_name"]
        if len(name) > 38:
            name = name[:37] + "…"
        print(f"{record['date']:<12}{name:<40}{record['spend']:>10}{record['impressions']:>8}"
              f"{record['taps']:>6}{record['installs_total']:>6}{record['new_downloads_total']:>6}"
              f"{record['redownloads_total']:>6}{record['avg_cpt']:>9}")


def write_csv(records: list[dict], start: dt.date, end: dt.date) -> pathlib.Path:
    REPORTS_DIR.mkdir(exist_ok=True)
    path = REPORTS_DIR / f"campaigns_{start.isoformat()}_{end.isoformat()}.csv"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(records)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--days", type=int, default=7, help="window ending today (default: %(default)s)")
    parser.add_argument("--start", type=parse_date, help="start date, YYYY-MM-DD")
    parser.add_argument("--end", type=parse_date, help="end date, YYYY-MM-DD")
    parser.add_argument("--granularity", default="DAILY",
                        choices=["HOURLY", "DAILY", "WEEKLY", "MONTHLY"])
    parser.add_argument("--csv", action="store_true", help=f"also write a CSV under {REPORTS_DIR.name}/")
    args = parser.parse_args()

    end = args.end or dt.date.today()
    start = args.start or (end - dt.timedelta(days=args.days - 1))

    if start > end:
        print(f"error: start {start} is after end {end}", file=sys.stderr)
        return 1
    span = (end - start).days + 1
    if args.granularity == "DAILY" and span > MAX_DAILY_RANGE:
        print(f"error: {span} days exceeds Apple's {MAX_DAILY_RANGE}-day cap for DAILY "
              "granularity; split the range or use --granularity WEEKLY", file=sys.stderr)
        return 1

    try:
        ad_account = resolve_ad_account_id()
        report = fetch(start, end, args.granularity)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    records = flatten(report)
    print(f"Apple Ads — campaigns, {start} to {end} ({args.granularity}), "
          f"ad account {ad_account}\n")

    if not records:
        print("No rows returned. Apple only reports days with delivery, so an empty "
              "result means no spend in this window (not an error).")
        return 0

    print_table(records)

    spend = sum(float(r["spend"] or 0) for r in records)
    taps = sum(int(r["taps"] or 0) for r in records)
    installs = sum(int(r["installs_total"] or 0) for r in records)
    currency = next((r["currency"] for r in records if r["currency"]), "")
    print(f"\n{len(records)} row(s) | spend {spend:,.2f} {currency} | taps {taps:,} | "
          f"installs {installs:,} | derived avg CPT {spend / taps if taps else 0:,.4f}")

    if args.csv:
        print(f"CSV written to {write_csv(records, start, end)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
