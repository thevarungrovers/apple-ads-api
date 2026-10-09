#!/usr/bin/env python3
"""Join attribution rows against the Apple ad structure -> which user came from which ad.

Input is a CSV of rows from `attribution_apple_search_ads` carrying at least
user_id / campaign_id / ad_group_id / keyword_id. Output is the same rows with
Apple's names attached, plus a verification pass over the join itself.

WHAT THE VERIFICATION CHECKS (this is the point of the script -- a join that
silently produces blanks is worse than one that fails loudly):

  1. every campaign_id / ad_group_id resolves to a live Apple object;
  2. every non-NULL keyword_id resolves;
  3. *** the keyword's OWN adGroupId matches the ad_group_id on the row. ***
     Keywords are namespaced per ad group, so this is a real cross-check and
     not a tautology: a row claiming keyword K under ad group G is only
     coherent if Apple also files K under G.
  4. a NULL keyword_id is only OK when Search Match can serve that ad group --
     either it has no targeting keywords at all, or it has keywords AND
     automatedKeywordsOptIn is on. A NULL under an ad group where Search Match
     is switched off is a genuine data gap and is reported as such, not
     quietly labelled.

  ./venv/bin/python resolve_attribution.py reports/attribution_rows.csv
  ./venv/bin/python resolve_attribution.py rows.csv --csv reports/out.csv
  ./venv/bin/python resolve_attribution.py rows.csv --by-ad-group
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import pathlib
import sys

from _bootstrap import ensure_venv

ensure_venv()  # re-exec under venv/ if launched with a bare `python3`

from apple_ads_mcp.config import ConfigError
from fetch_ad_structure import build_structure

HERE = pathlib.Path(__file__).resolve().parent
REPORTS_DIR = HERE / "reports"

SEARCH_MATCH = "(Search Match)"
UNRESOLVED = "(UNRESOLVED)"

OUT_COLUMNS = [
    "user_id", "campaign_name", "ad_group_name", "keyword", "match_type",
    "conversion_type", "claim_type", "click_date", "claimed_at",
    "campaign_id", "ad_group_id", "keyword_id",
]


def load_rows(path: pathlib.Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def as_int(value):
    value = (value or "").strip()
    return int(value) if value else None


def resolve(rows: list[dict], tree: dict) -> tuple[list[dict], list[str]]:
    """Attach names to each row. Returns (resolved_rows, problems)."""
    problems: list[str] = []
    out: list[dict] = []

    for row in rows:
        cid, gid, kid = (as_int(row.get(k)) for k in ("campaign_id", "ad_group_id", "keyword_id"))
        user = row.get("user_id")

        campaign = tree["campaigns"].get(str(cid))
        group = tree["ad_groups"].get(str(gid))
        keyword = tree["keywords"].get(str(kid)) if kid is not None else None

        if campaign is None:
            problems.append(f"user {user}: campaign_id {cid} not found in Apple")
        if group is None:
            problems.append(f"user {user}: ad_group_id {gid} not found in Apple")

        if kid is not None:
            if keyword is None:
                problems.append(f"user {user}: keyword_id {kid} not found in Apple")
            elif keyword["ad_group_id"] != gid:
                # The cross-check that makes this join trustworthy.
                problems.append(
                    f"user {user}: keyword {kid} ({keyword['text']!r}) belongs to ad group "
                    f"{keyword['ad_group_id']} ({keyword['ad_group_name']}), "
                    f"but the row says {gid}"
                )
        elif group is not None and not group.get("search_match_possible", group["search_match_only"]):
            problems.append(
                f"user {user}: NULL keyword_id under ad group {gid} ({group['name']}), "
                f"which targets {group['keyword_count']} keywords with Search Match OFF "
                f"-- nothing there can serve without a keyword"
            )

        if keyword is not None:
            keyword_label, match_type = keyword["text"], keyword["match_type"]
        elif kid is not None:
            keyword_label, match_type = UNRESOLVED, UNRESOLVED
        elif group is not None and group.get("search_match_possible", group["search_match_only"]):
            keyword_label, match_type = SEARCH_MATCH, "SEARCH_MATCH"
        else:
            keyword_label, match_type = "(none reported)", ""

        out.append({
            "user_id": user,
            "campaign_name": (campaign or {}).get("name", UNRESOLVED),
            "ad_group_name": (group or {}).get("name", UNRESOLVED),
            "keyword": keyword_label,
            "match_type": match_type,
            "conversion_type": row.get("conversion_type", ""),
            "claim_type": row.get("claim_type", ""),
            "click_date": row.get("click_date", ""),
            "claimed_at": row.get("claimed_at", ""),
            "campaign_id": cid,
            "ad_group_id": gid,
            "keyword_id": kid if kid is not None else "",
        })

    return out, problems


def print_table(rows: list[dict], columns: list[str]) -> None:
    widths = [max(len(str(r.get(c, ""))) for r in [{c: c for c in columns}, *rows]) for c in columns]
    print("  ".join(c.ljust(w) for c, w in zip(columns, widths)))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print("  ".join(str(row.get(c, "")).ljust(w) for c, w in zip(columns, widths)))


def print_summary(rows: list[dict]) -> None:
    counter = collections.Counter(
        (r["ad_group_name"], r["keyword"], r["match_type"]) for r in rows
    )
    summary = [
        {"ad_group_name": g, "keyword": k, "match_type": m, "users": n}
        for (g, k, m), n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    print_table(summary, ["ad_group_name", "keyword", "match_type", "users"])
    print(f"\n{sum(s['users'] for s in summary)} users across {len(summary)} ad group / keyword pairs")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resolve attribution_apple_search_ads rows to Apple ad group / keyword names.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("csv_path", type=pathlib.Path, help="CSV of attribution rows")
    parser.add_argument("--campaign", action="append", type=int, default=None,
                        help="limit the Apple fetch to one campaign id (repeatable)")
    parser.add_argument("--by-ad-group", action="store_true",
                        help="print the per-ad-group/keyword summary instead of per-user rows")
    parser.add_argument("--csv", type=pathlib.Path, default=None, help="write the joined rows to a CSV")
    parser.add_argument("--structure", type=pathlib.Path, default=None,
                        help="reuse a saved ad_structure.json instead of calling Apple")
    args = parser.parse_args()

    if not args.csv_path.exists():
        print(f"error: {args.csv_path} not found", file=sys.stderr)
        return 1

    rows = load_rows(args.csv_path)

    try:
        if args.structure:
            tree = json.loads(args.structure.read_text())
        else:
            tree = build_structure(args.campaign)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    resolved, problems = resolve(rows, tree)

    if args.by_ad_group:
        print_summary(resolved)
    else:
        print_table(resolved, OUT_COLUMNS)
        print(f"\n{len(resolved)} rows")

    print(file=sys.stderr)
    if problems:
        print(f"VERIFICATION: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
    else:
        print("VERIFICATION: all ids resolved; every keyword sits in the ad group "
              "its row claims; every NULL keyword_id is an ad group Search Match "
              "can serve.", file=sys.stderr)

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        with args.csv.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
            writer.writeheader()
            writer.writerows(resolved)
        print(f"wrote {args.csv}", file=sys.stderr)

    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
