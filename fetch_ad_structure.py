#!/usr/bin/env python3
"""Pull the campaign -> ad group -> keyword tree and write it as a lookup table.

`attribution_apple_search_ads` stores only Apple's numeric ids (campaign_id,
ad_group_id, keyword_id). This script turns those into the names a human reads
in the Apple Ads UI, so an attribution row can be reported on without anyone
hand-maintaining a mapping.

Two things about the Apple side shape the output:

- Keywords are namespaced PER AD GROUP. There is no org-wide "get keyword by
  id" endpoint, so resolving a keywordId means walking every ad group of the
  campaign and building the map. That also means a (adGroupId, keywordId) pair
  from an attribution row can be cross-checked: if the keyword turns up under a
  different ad group than the row claims, the row is wrong.
- A Search Match ad group returns an EMPTY keyword list. Its traffic carries no
  keywordId at all, so a NULL keyword_id in the attribution table is correct
  data, not a lookup failure. Those rows are labelled `(Search Match)`.

  ./venv/bin/python fetch_ad_structure.py                     # table to stdout
  ./venv/bin/python fetch_ad_structure.py --json              # lookup as JSON
  ./venv/bin/python fetch_ad_structure.py --campaign 1234567890

--campaign defaults to APPLE_ADS_DEFAULT_CAMPAIGN_ID when that is set in .env,
so the real id never has to be typed into a command or a doc.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from _bootstrap import ensure_venv

ensure_venv()  # re-exec under venv/ if launched with a bare `python3`

from apple_ads_mcp.client import (
    context_header,
    enum_value,
    eq_filter,
    query,
    unwrap,
)
from apple_ads_mcp.client import call as api_call
from apple_ads_mcp.config import ConfigError, load_config, resolve_ad_account_id

HERE = pathlib.Path(__file__).resolve().parent
REPORTS_DIR = HERE / "reports"

# Apple sends -1 in ad_id when the placement has no distinct creative, which is
# every Search Results row. It is a sentinel, not a real id -- never look it up.
NO_AD_SENTINEL = -1


def _rows(method: str, what: str, filters=None) -> list:
    """One *_query_post call, unwrapped. page_size 1000 is Apple's ceiling."""
    return unwrap(
        api_call(
            method,
            x_ap_context=context_header(),
            query_request=query(page_size=1000, filters=filters),
        ),
        what,
    ) or []


def build_structure(campaign_ids: list[int] | None = None) -> dict:
    """Walk campaigns -> ad groups -> keywords and return a flat lookup tree.

    Platform API notes, all of which differ from v5:
      - `servingStatus` is now `displayStatus`.
      - supplySources / countriesOrRegions moved under `targeting` and became
        SINGULAR (supplyPlacement, countryOrRegion), each shaped {include: [...]}.
      - Keywords are fetched by adGroupId filter rather than from a nested path.
      - Enums deserialize to Python enum members, so anything written into the
        JSON tree goes through enum_value() -- otherwise the saved file holds
        "KeywordStatus.ENABLED" instead of "ENABLED" and every downstream string
        comparison quietly fails.
    """
    campaigns = _rows("campaigns_query_post", "campaigns")
    if campaign_ids:
        wanted = {int(c) for c in campaign_ids}
        campaigns = [c for c in campaigns if int(c.id) in wanted]

    tree: dict = {"campaigns": {}, "ad_groups": {}, "keywords": {}}

    for campaign in campaigns:
        cid = int(campaign.id)
        targeting = getattr(campaign, "targeting", None)
        tree["campaigns"][str(cid)] = {
            "id": cid,
            "name": campaign.name,
            "status": enum_value(campaign.status),
            "serving_status": enum_value(campaign.display_status),
            "supply_sources": _included(getattr(targeting, "supply_placement", None)),
            "countries_or_regions": _included(getattr(targeting, "country_or_region", None)),
        }

        for group in _rows("adgroups_query_post", "ad groups", [eq_filter("campaignId", cid)]):
            gid = int(group.id)
            keywords = _rows("keywords_query_post", "keywords", [eq_filter("adGroupId", gid)])
            tree["ad_groups"][str(gid)] = {
                "id": gid,
                "name": group.name,
                "campaign_id": cid,
                "campaign_name": campaign.name,
                "status": enum_value(group.status),
                "serving_status": enum_value(group.display_status),
                # An ad group with no targeting keywords runs on Search Match
                # alone. But Search Match can ALSO be switched on alongside
                # explicit keywords (automatedKeywordsOptIn), and then a tap in
                # a keyword-targeted ad group legitimately carries no keywordId.
                # Both cases have to count as "a NULL keyword_id is expected
                # here", or the join reports correct data as broken.
                "keyword_count": len(keywords),
                "search_match_only": len(keywords) == 0,
                "search_match_opt_in": bool(group.automated_keywords_opt_in),
                "search_match_possible": len(keywords) == 0 or bool(group.automated_keywords_opt_in),
            }

            for keyword in keywords:
                kid = int(keyword.id)
                tree["keywords"][str(kid)] = {
                    "id": kid,
                    "text": keyword.text,
                    "match_type": enum_value(keyword.match_type),
                    "status": enum_value(keyword.status),
                    "ad_group_id": gid,
                    "ad_group_name": group.name,
                    "campaign_id": cid,
                    "campaign_name": campaign.name,
                }

    return tree


def _included(targeting_data) -> list | None:
    """The `include` list out of a Platform API targeting block."""
    if targeting_data is None:
        return None
    values = getattr(targeting_data, "include", None)
    return [enum_value(v) for v in values] if values else None


def print_table(tree: dict) -> None:
    rows = []
    for group in sorted(tree["ad_groups"].values(), key=lambda g: (g["campaign_name"] or "", g["name"] or "")):
        kws = [k for k in tree["keywords"].values() if k["ad_group_id"] == group["id"]]
        if not kws:
            rows.append((group["campaign_name"], group["name"], "(Search Match)", "-", "-"))
            continue
        if group["search_match_opt_in"]:
            rows.append((group["campaign_name"], group["name"], "(Search Match)", "-", "-"))
        for kw in sorted(kws, key=lambda k: (k["text"] or "")):
            rows.append((group["campaign_name"], group["name"], kw["text"], kw["match_type"], str(kw["id"])))

    headers = ("campaign", "ad_group", "keyword", "match_type", "keyword_id")
    widths = [max(len(str(r[i])) for r in [headers, *rows]) for i in range(len(headers))]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    print(line)
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(c).ljust(w) for c, w in zip(row, widths)))
    print(f"\n{len(tree['campaigns'])} campaigns, {len(tree['ad_groups'])} ad groups, "
          f"{len(tree['keywords'])} keywords")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch the campaign/ad group/keyword tree from Apple Search Ads.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--campaign", action="append", type=int, default=None,
                        help="limit to one campaign id (repeatable). Defaults to "
                             "APPLE_ADS_DEFAULT_CAMPAIGN_ID from .env when set.")
    parser.add_argument("--json", action="store_true", help="print the lookup tree as JSON")
    parser.add_argument("--save", action="store_true",
                        help="also write reports/ad_structure.json")
    args = parser.parse_args()

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    campaign_ids = args.campaign
    if campaign_ids is None:
        # The real campaign id lives in .env, never in source or in a README. An
        # absent key is not an error -- without it the script walks every campaign.
        default = (config.get("APPLE_ADS_DEFAULT_CAMPAIGN_ID") or "").strip()
        if default:
            campaign_ids = [int(default)]

    try:
        resolve_ad_account_id(config)
        tree = build_structure(campaign_ids)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(tree, indent=2))
    else:
        print_table(tree)

    if args.save:
        REPORTS_DIR.mkdir(exist_ok=True)
        path = REPORTS_DIR / "ad_structure.json"
        path.write_text(json.dumps(tree, indent=2))
        print(f"\nwrote {path}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
