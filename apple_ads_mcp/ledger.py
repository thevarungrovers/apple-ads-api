"""Append-only JSONL record of every write the server attempts.

`.audit/changes.jsonl`, mode 600, `.audit/` gitignored because the entries carry
real entity ids.

TWO LINES PER WRITE, linked by `entry_id`:

  - an `intent` line written BEFORE the API call leaves the process, and
  - an `outcome` line written after it returns.

The pair is the whole point. A single line written afterwards records only the
writes that came back, and the case that actually needs evidence is the one that
did not: a crash, or a timeout, where the mutation may well have landed at
Apple's end and nothing local would ever say so. An `intent` with no `outcome`
is exactly the signal "something may have changed; go and look".

Appends are `open(path, "a")` plus a single `json.dumps(...) + "\\n"` under
`fcntl.flock`, so a concurrent `apple_ads_client.py --apply` run writing to the
same file cannot interleave half a record into the middle of ours.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterator

from apple_ads_mcp.guardrails import AUDIT_DIR, ensure_audit_dir

LEDGER_PATH = AUDIT_DIR / "changes.jsonl"

INTENT = "intent"
OUTCOME = "outcome"

APPLIED = "applied"
FAILED = "failed"
PARTIAL = "partial"
REFUSED = "refused"


def new_entry_id() -> str:
    return uuid.uuid4().hex


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _append(record: dict[str, Any]) -> None:
    ensure_audit_dir()
    existed = LEDGER_PATH.exists()
    line = json.dumps(record, default=str, ensure_ascii=False) + "\n"
    # "a" plus flock: the lock serialises us against another process, and the
    # single write of one already-complete line is what keeps a record atomic.
    with LEDGER_PATH.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    if not existed:
        try:
            os.chmod(LEDGER_PATH, 0o600)
        except OSError:
            pass


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
    _append(
        {
            "entry_id": entry_id,
            "record": INTENT,
            "ts": _now(),
            "tool": tool,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "entity_name": entity_name,
            "path": path,
            "field": field,
            "before": before,
            "after": after,
            "campaign_id": campaign_id,
            "projected_daily_spend_delta": projected_daily_spend_delta,
            "reverts_entry_id": reverts_entry_id,
            **(extra or {}),
        }
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
    _append(
        {
            "entry_id": entry_id,
            "record": OUTCOME,
            "ts": _now(),
            "outcome": outcome,
            "detail": detail,
            "observed_after": observed_after,
            "failures": failures or [],
        }
    )


def read_records(path: Path | None = None) -> Iterator[dict[str, Any]]:
    """Every line, oldest first. A corrupt line is skipped, never fatal."""
    target = path or LEDGER_PATH
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


def entries(since: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Intent lines merged with their outcome, newest first.

    An entry with `outcome: None` is the dangerous one: the write was started and
    nothing ever recorded how it ended.
    """
    merged: dict[str, dict[str, Any]] = {}
    for record in read_records():
        entry_id = record.get("entry_id")
        if not entry_id:
            continue
        if record.get("record") == INTENT:
            merged.setdefault(entry_id, {}).update(
                {k: v for k, v in record.items() if k != "record"}
            )
            merged[entry_id].setdefault("outcome", None)
        elif record.get("record") == OUTCOME:
            target = merged.setdefault(entry_id, {"entry_id": entry_id})
            target["outcome"] = record.get("outcome")
            target["outcome_ts"] = record.get("ts")
            target["outcome_detail"] = record.get("detail")
            target["observed_after"] = record.get("observed_after")
            target["failures"] = record.get("failures") or []

    rows = list(merged.values())
    if since is not None:
        cutoff = since.isoformat(timespec="seconds")
        rows = [r for r in rows if str(r.get("ts", "")) >= cutoff]
    rows.sort(key=lambda r: str(r.get("ts", "")), reverse=True)
    return rows


def find_entry(entry_id: str) -> dict[str, Any] | None:
    for entry in entries():
        if entry.get("entry_id") == entry_id:
            return entry
    return None


def reverted_entry_ids() -> set[str]:
    """Entries that some later, successful entry already reverted."""
    done = set()
    for entry in entries():
        target = entry.get("reverts_entry_id")
        if target and entry.get("outcome") == APPLIED:
            done.add(target)
    return done
