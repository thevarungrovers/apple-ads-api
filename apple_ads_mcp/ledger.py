"""The mutation ledger: what this server changed, and how to undo one.

The storage moved to SQLite (`logs/apple-ads.db`, see `apple_ads_mcp.logdb`).
This module stayed, as the ledger-shaped face of that database, because
`tools_write.py` and `tools_read.py` speak in writes and entries rather than
tables -- and because the `entry` dict it hands back is a contract that
`revert_change` reads field by field.

WHAT DID NOT CHANGE: the two-phase record. `write_intent` goes in BEFORE the
API call leaves the process and `write_outcome` after it returns. An entry
whose outcome is None is the one that matters -- the write started and nothing
recorded how it ended, so it may have landed at Apple's end and nothing local
would ever say so. `reconcile_ledger` exists to answer exactly that.

WHAT DID: the record is a row, not a line, so an outcome is an UPDATE of the
intent rather than a second line merged on read. `entries()` no longer folds
pairs together; SQL does it.

The retired `.audit/changes.jsonl` is still on disk, frozen, and nothing writes
to it. `import_legacy_jsonl()` is what moved its contents in here; it is
idempotent, so re-running it after a restore cannot double-count.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Iterator

from apple_ads_mcp import logdb
from apple_ads_mcp.guardrails import AUDIT_DIR

#: The retired JSONL. Read for migration, never written.
LEGACY_LEDGER_PATH = AUDIT_DIR / "changes.jsonl"

INTENT = "intent"
OUTCOME = "outcome"

APPLIED = logdb.APPLIED
FAILED = logdb.FAILED
PARTIAL = logdb.PARTIAL
REFUSED = logdb.REFUSED

new_entry_id = logdb.new_entry_id
entries = logdb.entries
find_entry = logdb.find_entry
reverted_entry_ids = logdb.reverted_entry_ids


def write_intent(
    entry_id: str,
    *,
    tool: str,
    entity_type: str,
    entity_id: int | str,
    entity_name: str | None,
    path: str,
    field: str,
    before: Any,
    after: Any,
    campaign_id: int | None,
    projected_daily_spend_delta: str,
    reverts_entry_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Record what we are ABOUT to do. Must be called before the API call."""
    logdb.insert_change(
        entry_id,
        tool=tool,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        path=path,
        field=field,
        before=before,
        after=after,
        campaign_id=campaign_id,
        projected_daily_spend_delta=projected_daily_spend_delta,
        reverts_entry_id=reverts_entry_id,
        extra=extra,
    )


def write_outcome(
    entry_id: str,
    *,
    outcome: str,
    detail: str = "",
    observed_after: Any = None,
    failures: list[dict[str, Any]] | None = None,
) -> None:
    """Record how it went. `outcome` is one of applied/failed/partial/refused."""
    logdb.complete_change(
        entry_id,
        outcome=outcome,
        detail=detail,
        observed_after=observed_after,
        failures=failures,
    )


# --- migration off the JSONL ----------------------------------------------


def read_legacy_records(path: Path | None = None) -> Iterator[dict[str, Any]]:
    """Every line of the retired JSONL, oldest first. A corrupt line is skipped."""
    target = path or LEGACY_LEDGER_PATH
    if not target.exists():
        return
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def fold_legacy_records(path: Path | None = None) -> list[dict[str, Any]]:
    """Intent lines merged with their outcome, oldest first.

    The JSONL's own read-time fold, kept here because it is the only thing that
    understands the two-line format now.
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for record in read_legacy_records(path):
        entry_id = record.get("entry_id")
        if not entry_id:
            continue
        if entry_id not in merged:
            merged[entry_id] = {"entry_id": entry_id}
            order.append(entry_id)
        if record.get("record") == INTENT:
            merged[entry_id].update({k: v for k, v in record.items() if k != "record"})
            merged[entry_id].setdefault("outcome", None)
        elif record.get("record") == OUTCOME:
            merged[entry_id].update(
                {
                    "outcome": record.get("outcome"),
                    "outcome_ts": record.get("ts"),
                    "outcome_detail": record.get("detail"),
                    "observed_after": record.get("observed_after"),
                    "failures": record.get("failures") or [],
                }
            )
    return [merged[entry_id] for entry_id in order]


#: Keys the `changes` table has columns for. Anything else in a legacy record
#: was passed through `extra=` and belongs in extra_json.
_KNOWN_KEYS = {
    "entry_id",
    "record",
    "ts",
    "tool",
    "entity_type",
    "entity_id",
    "entity_name",
    "path",
    "field",
    "before",
    "after",
    "campaign_id",
    "projected_daily_spend_delta",
    "reverts_entry_id",
    "outcome",
    "outcome_ts",
    "outcome_detail",
    "observed_after",
    "failures",
}


def import_legacy_jsonl(path: Path | None = None) -> dict[str, int]:
    """Copy the retired JSONL into the database. Idempotent.

    An entry_id already present is left exactly as it is rather than refreshed:
    the database is the live record now, so a re-run after new writes must not
    roll one back to its state at migration time.
    """
    conn = logdb.connect()
    folded = fold_legacy_records(path)
    inserted = 0
    skipped = 0
    for entry in folded:
        entry_id = str(entry["entry_id"])
        existing = conn.execute(
            "SELECT 1 FROM changes WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if existing is not None:
            skipped += 1
            continue
        extra = {k: v for k, v in entry.items() if k not in _KNOWN_KEYS}
        conn.execute(
            "INSERT INTO changes"
            "(entry_id, ts, tool, entity_type, entity_id, entity_name, path, field, "
            " before, after, campaign_id, projected_daily_spend_delta, reverts_entry_id, "
            " extra_json, outcome, outcome_ts, outcome_detail, observed_after, failures_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry_id,
                entry.get("ts") or "",
                entry.get("tool") or "",
                entry.get("entity_type") or "",
                None if entry.get("entity_id") is None else str(entry.get("entity_id")),
                entry.get("entity_name"),
                entry.get("path"),
                entry.get("field"),
                None if entry.get("before") is None else str(entry.get("before")),
                None if entry.get("after") is None else str(entry.get("after")),
                entry.get("campaign_id"),
                entry.get("projected_daily_spend_delta"),
                entry.get("reverts_entry_id"),
                json.dumps(extra, default=str) if extra else None,
                entry.get("outcome"),
                entry.get("outcome_ts"),
                entry.get("outcome_detail"),
                None
                if entry.get("observed_after") is None
                else str(entry.get("observed_after")),
                json.dumps(entry.get("failures") or [], default=str),
            ),
        )
        inserted += 1
    return {"inserted": inserted, "already_present": skipped, "read": len(folded)}


def legacy_entries(since: dt.datetime | None = None) -> list[dict[str, Any]]:
    """The old read path, kept so a migration can be verified against its source."""
    rows = fold_legacy_records()
    if since is not None:
        cutoff = since.isoformat(timespec="seconds")
        rows = [r for r in rows if str(r.get("ts", "")) >= cutoff]
    rows.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return rows
