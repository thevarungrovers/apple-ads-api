#!/usr/bin/env python3
"""Move the retired `.audit/changes.jsonl` ledger into `logs/apple-ads.db`.

    ./venv/bin/python migrate_ledger.py            # show what would move
    ./venv/bin/python migrate_ledger.py --apply    # move it
    ./venv/bin/python migrate_ledger.py --verify   # compare the two afterwards
    ./venv/bin/python migrate_ledger.py --apply --backups   # include changes.jsonl.*

Idempotent: an entry_id already in the database is left exactly as it is, so
re-running after new writes cannot roll one back to its state at migration
time, and cannot double-count.

The JSONL is NOT deleted. It stays on disk, frozen and unwritten, as the thing
to fall back to if the database is ever lost -- which is the whole reason this
script also has a `--verify` mode. Nothing appends to it any more.
"""

from __future__ import annotations

import argparse
import sys

from _bootstrap import ensure_venv

ensure_venv()

from apple_ads_mcp import ledger, logdb  # noqa: E402


def preview() -> int:
    folded = ledger.fold_legacy_records()
    if not folded:
        print(f"Nothing to migrate: {ledger.LEGACY_LEDGER_PATH} is absent or empty.")
        return 0
    conn = logdb.connect()
    already = 0
    for entry in folded:
        row = conn.execute(
            "SELECT 1 FROM changes WHERE entry_id=?", (str(entry["entry_id"]),)
        ).fetchone()
        if row is not None:
            already += 1
    print(f"source: {ledger.LEGACY_LEDGER_PATH}")
    print(f"target: {logdb.DB_PATH}")
    print(f"\n  {len(folded)} entries in the JSONL")
    print(f"  {already} already in the database")
    print(f"  {len(folded) - already} would be inserted")
    unfinished = sum(1 for e in folded if e.get("outcome") is None)
    if unfinished:
        print(f"\n  note: {unfinished} entr{'y' if unfinished == 1 else 'ies'} "
              f"have no outcome -- they migrate as unfinished, which is correct.")

    backups = ledger.legacy_backup_paths()
    if backups:
        print("\nbackup files beside it (--backups to include):")
        for path in backups:
            orphans = [
                entry
                for entry in ledger.fold_legacy_records(path)
                if logdb.find_entry(str(entry["entry_id"])) is None
            ]
            print(f"  {path.name}: {len(orphans)} entr"
                  f"{'y' if len(orphans) == 1 else 'ies'} in no other file")
            for entry in orphans:
                outcome, _ = ledger.reclassify_outcome(entry)
                arrow = (
                    f"  {entry.get('outcome')} -> {outcome}"
                    if outcome != entry.get("outcome")
                    else f"  {entry.get('outcome')}"
                )
                print(f"    {str(entry['entry_id'])[:8]}  {entry.get('tool')}"
                      f"  {entry.get('after')!r} vs observed "
                      f"{entry.get('observed_after')!r}{arrow}")

    print("\nAdd --apply to do it.")
    return 0


def apply(include_backups: bool = False) -> int:
    result = ledger.import_legacy_jsonl()
    print(f"read {result['read']} entries from {ledger.LEGACY_LEDGER_PATH}")
    print(f"inserted {result['inserted']}, already present {result['already_present']}")

    if include_backups:
        for path in ledger.legacy_backup_paths():
            # reclassify=True only here. The live ledger was written by code
            # that already ran the read-back check; these backups predate it.
            backup = ledger.import_legacy_jsonl(path, reclassify=True)
            print(f"\nread {backup['read']} entries from {path.name}")
            print(f"inserted {backup['inserted']}, "
                  f"already present {backup['already_present']}, "
                  f"reclassified {backup['reclassified']}")

    print("\nThe JSONL was not deleted. Nothing writes to it any more.")
    return 0


def verify() -> int:
    """Every JSONL entry must now be in the database, with the same outcome."""
    folded = ledger.fold_legacy_records()
    missing = []
    mismatched = []
    for entry in folded:
        entry_id = str(entry["entry_id"])
        row = logdb.find_entry(entry_id)
        if row is None:
            missing.append(entry_id)
            continue
        if row.get("outcome") != entry.get("outcome"):
            mismatched.append((entry_id, entry.get("outcome"), row.get("outcome")))

    print(f"{len(folded)} entries in the JSONL, {len(logdb.entries())} rows in the database")
    if missing:
        print(f"\nMISSING from the database ({len(missing)}):")
        for entry_id in missing:
            print(f"  {entry_id}")
    if mismatched:
        print(f"\nOUTCOME DIFFERS ({len(mismatched)}):")
        for entry_id, was, now in mismatched:
            print(f"  {entry_id}: jsonl={was!r} db={now!r}")
    if not missing and not mismatched:
        print("\nEvery JSONL entry is present with the same outcome.")
        return 0
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--apply", action="store_true", help="perform the migration")
    group.add_argument(
        "--verify", action="store_true", help="check the database against the JSONL"
    )
    parser.add_argument(
        "--backups",
        action="store_true",
        help="also migrate .audit/changes.jsonl.* backups, re-deriving each "
             "entry's outcome from its own read-back",
    )
    args = parser.parse_args()

    if args.apply:
        return apply(include_backups=args.backups)
    if args.verify:
        return verify()
    return preview()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
