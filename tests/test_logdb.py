#!/usr/bin/env python3
"""The log database is now the only record of a real-money mutation. Pin it.

Two properties matter more than the rest, and both are the kind that pass by
accident on a first run:

  - An outcome must UPDATE its intent row without touching what the intent
    recorded. The upsert form of this is a known silent-overwrite trap, so the
    test applies an outcome TWICE and asserts the intent columns are still
    there -- a single pass would only exercise the INSERT.

  - Observability must never raise and the ledger must always raise. Those are
    opposite requirements on the same module, so both are asserted against a
    database that cannot be opened at all.

Run it without touching Apple:
    ./venv/bin/python tests/test_logdb.py
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import sys
import tempfile
import threading

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apple_ads_mcp import ledger, logdb  # noqa: E402

_TEMP_DIRS: list[tempfile.TemporaryDirectory] = []


def use_temp_db() -> pathlib.Path:
    """Point the module at a fresh database and forget the cached connection.

    The connection is cached per thread and the session row per process, so
    both have to be cleared or a test would keep writing to its predecessor's
    file.
    """
    tmp = tempfile.TemporaryDirectory()
    _TEMP_DIRS.append(tmp)
    root = pathlib.Path(tmp.name)
    logdb.LOG_DIR = root / "logs"
    logdb.DB_PATH = logdb.LOG_DIR / "apple-ads.db"
    logdb._local = threading.local()
    logdb._session_id = None
    return logdb.DB_PATH


def break_db() -> None:
    """Point the module somewhere it cannot possibly open a database."""
    logdb.LOG_DIR = pathlib.Path("/dev/null/not-a-directory")
    logdb.DB_PATH = logdb.LOG_DIR / "apple-ads.db"
    logdb._local = threading.local()
    logdb._session_id = None


def an_intent(entry_id: str, **overrides) -> None:
    fields = dict(
        tool="apply_keyword_bid",
        entity_type="keyword",
        entity_id=123,
        entity_name="organic kale",
        path="Campaign 'C' > AdGroup 'A' > Keyword 'organic kale'",
        field="bid",
        before="1.00",
        after="2.00",
        campaign_id=99,
        projected_daily_spend_delta="5.00",
    )
    fields.update(overrides)
    logdb.insert_change(entry_id, **fields)


# --- schema and pragmas ----------------------------------------------------


def test_connect_creates_the_schema_and_sets_the_pragmas():
    use_temp_db()
    conn = logdb.connect()
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tool_calls", "api_calls", "changes", "sessions", "meta"} <= tables, tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # 2 is FULL. The ledger half of this file is evidence, not telemetry.
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2


def test_the_database_file_is_not_world_readable():
    path = use_temp_db()
    logdb.connect()
    assert oct(path.stat().st_mode)[-3:] == "600", oct(path.stat().st_mode)


# --- the two-phase ledger --------------------------------------------------


def test_an_intent_with_no_outcome_reads_back_as_unfinished():
    use_temp_db()
    an_intent("aaa")
    entry = logdb.find_entry("aaa")
    assert entry is not None
    assert entry["outcome"] is None, entry
    assert entry["before"] == "1.00" and entry["after"] == "2.00"


def test_an_outcome_updates_its_intent_row():
    use_temp_db()
    an_intent("bbb")
    logdb.complete_change("bbb", outcome=logdb.APPLIED, observed_after="2.00")
    entry = logdb.find_entry("bbb")
    assert entry["outcome"] == "applied"
    assert entry["observed_after"] == "2.00"
    assert len(logdb.entries()) == 1, "the outcome must update, not insert a second row"


def test_a_second_outcome_does_not_wipe_the_intent():
    """The upsert trap, as a regression test.

    `excluded.col` is the value that WOULD have been inserted, so an upsert
    written to preserve the intent columns overwrites them instead -- and only
    on the SECOND pass, because the first is an INSERT and never reaches the
    DO UPDATE clause. So: apply an outcome twice and check the intent survived.
    """
    use_temp_db()
    an_intent("ccc")
    logdb.complete_change("ccc", outcome=logdb.APPLIED, observed_after="2.00")
    logdb.complete_change("ccc", outcome=logdb.FAILED, detail="second pass")

    entry = logdb.find_entry("ccc")
    assert entry["tool"] == "apply_keyword_bid", entry
    assert entry["entity_id"] == "123", entry
    assert entry["before"] == "1.00" and entry["after"] == "2.00", entry
    assert entry["campaign_id"] == 99, entry
    assert entry["projected_daily_spend_delta"] == "5.00", entry
    assert entry["outcome"] == "failed" and entry["outcome_detail"] == "second pass"
    assert len(logdb.entries()) == 1


def test_entries_come_back_newest_first():
    use_temp_db()
    for index, entry_id in enumerate(["old", "mid", "new"]):
        an_intent(entry_id)
        logdb.connect().execute(
            "UPDATE changes SET ts=? WHERE entry_id=?",
            (f"2026-10-0{index + 1}T00:00:00+00:00", entry_id),
        )
    assert [e["entry_id"] for e in logdb.entries()] == ["new", "mid", "old"]


def test_find_entry_returns_none_for_an_unknown_id():
    use_temp_db()
    assert logdb.find_entry("nope") is None


def test_only_an_applied_revert_counts_as_having_reverted_something():
    use_temp_db()
    an_intent("target-1")
    an_intent("target-2")
    an_intent("revert-ok", tool="revert_change", reverts_entry_id="target-1")
    logdb.complete_change("revert-ok", outcome=logdb.APPLIED)
    an_intent("revert-failed", tool="revert_change", reverts_entry_id="target-2")
    logdb.complete_change("revert-failed", outcome=logdb.FAILED)

    assert logdb.reverted_entry_ids() == {"target-1"}, (
        "a revert that failed must not mark its target as already reverted"
    )


def test_extra_is_spliced_back_to_the_top_level():
    use_temp_db()
    an_intent("ddd", extra={"items": 3, "note": "bulk"})
    entry = logdb.find_entry("ddd")
    assert entry["items"] == 3 and entry["note"] == "bulk", entry


# --- tool calls and their api calls ----------------------------------------


def test_an_api_call_names_the_tool_call_that_caused_it():
    use_temp_db()
    with logdb.record_tool_call("list_campaigns", {"include_deleted": False}):
        with logdb.record_api_call("campaigns_query_post", request={"a": 1}):
            pass
        with logdb.record_api_call("campaigns_query_post", request={"a": 2}):
            pass

    conn = logdb.connect()
    tool_row = conn.execute("SELECT * FROM tool_calls").fetchone()
    assert tool_row["tool_name"] == "list_campaigns"
    assert tool_row["ok"] == 1
    assert json.loads(tool_row["args_json"]) == {"include_deleted": False}

    api_rows = conn.execute("SELECT * FROM api_calls ORDER BY id").fetchall()
    assert len(api_rows) == 2
    assert {row["tool_call_id"] for row in api_rows} == {tool_row["id"]}, (
        "both API calls must point at the tool call that caused them"
    )


def test_an_api_call_outside_a_tool_call_has_no_parent():
    use_temp_db()
    with logdb.record_api_call("get_me"):
        pass
    row = logdb.connect().execute("SELECT * FROM api_calls").fetchone()
    assert row["tool_call_id"] is None


def test_the_context_var_is_reset_after_the_tool_call():
    use_temp_db()
    with logdb.record_tool_call("whoami"):
        pass
    with logdb.record_api_call("get_me"):
        pass
    row = logdb.connect().execute("SELECT * FROM api_calls").fetchone()
    assert row["tool_call_id"] is None, "a later API call must not inherit a finished tool call"


def test_a_failing_tool_is_recorded_and_still_raises():
    use_temp_db()
    try:
        with logdb.record_tool_call("apply_keyword_bid"):
            raise ValueError("boom")
    except ValueError:
        pass
    else:
        raise AssertionError("the exception must propagate")

    row = logdb.connect().execute("SELECT * FROM tool_calls").fetchone()
    assert row["ok"] == 0
    assert "boom" in row["error"]
    assert row["duration_ms"] is not None


def test_an_http_status_is_recorded_from_the_slot():
    use_temp_db()
    with logdb.record_api_call("PUT", path="campaigns/1") as slot:
        slot["http_status"] = 200
    row = logdb.connect().execute("SELECT * FROM api_calls").fetchone()
    assert row["http_status"] == 200 and row["ok"] == 1


def test_an_oversized_payload_is_truncated_not_dropped():
    use_temp_db()
    with logdb.record_api_call("report", request={"blob": "x" * (logdb.MAX_JSON_CHARS * 2)}):
        pass
    row = logdb.connect().execute("SELECT * FROM api_calls").fetchone()
    assert row["request_json"].endswith("chars]"), row["request_json"][-40:]
    assert len(row["request_json"]) < logdb.MAX_JSON_CHARS + 100


# --- the two error policies ------------------------------------------------


def test_observability_never_raises_even_when_the_database_cannot_be_opened():
    break_db()
    with logdb.record_tool_call("whoami", {"a": 1}):
        with logdb.record_api_call("get_me", request={"b": 2}):
            pass
    # Reaching here at all is the assertion: a logging failure must not become
    # a tool failure, because the server's stderr is invisible in normal use.


def test_a_tool_body_still_runs_when_logging_is_broken():
    break_db()
    ran = []
    with logdb.record_tool_call("whoami"):
        ran.append(True)
    assert ran == [True]


def test_the_ledger_does_raise_when_it_cannot_be_written():
    break_db()
    try:
        an_intent("eee")
    except Exception:
        return
    raise AssertionError(
        "a lost mutation record must fail loudly: it is the only record there is"
    )


# --- concurrency -----------------------------------------------------------


def test_a_second_process_can_write_while_the_first_holds_the_database():
    """WAL plus busy_timeout is what makes the CLI and the server coexist."""
    path = use_temp_db()
    logdb.connect()
    an_intent("first")

    other = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    other.execute("PRAGMA busy_timeout=10000")
    other.execute(
        "INSERT INTO changes(entry_id, ts, tool, entity_type) VALUES('second','t','cli','keyword')"
    )
    other.close()

    assert {e["entry_id"] for e in logdb.entries()} == {"first", "second"}


def test_a_worker_thread_gets_its_own_connection():
    use_temp_db()
    errors: list[BaseException] = []

    def write() -> None:
        try:
            an_intent("from-thread")
        except BaseException as exc:  # pragma: no cover - only on failure
            errors.append(exc)

    thread = threading.Thread(target=write)
    thread.start()
    thread.join()
    assert not errors, errors
    assert logdb.find_entry("from-thread") is not None


# --- the tool wrapper ------------------------------------------------------


def test_instrumenting_a_tool_preserves_its_signature_and_annotations():
    import inspect

    def sample(count: int = 3) -> str:
        """Docstring kept."""
        return "x" * count

    wrapped = logdb.instrument_tool(sample)
    assert wrapped.__name__ == "sample"
    assert wrapped.__doc__ == "Docstring kept."

    # Parameter names and defaults must survive verbatim -- the SDK builds the
    # input schema from them.
    original = inspect.signature(sample)
    wrapper_sig = inspect.signature(wrapped)
    assert list(wrapper_sig.parameters) == list(original.parameters)
    assert wrapper_sig.parameters["count"].default == 3

    # The annotations must NOT survive verbatim: this module uses
    # `from __future__ import annotations`, so the original's are strings and
    # the wrapper's must be the resolved classes, or pydantic would try to look
    # them up in logdb's globals and fail.
    assert original.parameters["count"].annotation == "int"
    assert wrapper_sig.parameters["count"].annotation is int
    assert wrapper_sig.return_annotation is str
    assert wrapped.__annotations__["return"] is str
    use_temp_db()
    assert wrapped(2) == "xx"
    assert logdb.connect().execute("SELECT tool_name FROM tool_calls").fetchone()[0] == "sample"


def test_every_registered_tool_was_instrumented():
    """UNINSTRUMENTED is the silent-fallback escape hatch. It must stay empty."""
    import apple_ads_mcp.server  # noqa: F401 -- importing registers all 38 tools

    assert logdb.UNINSTRUMENTED == [], (
        f"these tools are not being logged: {logdb.UNINSTRUMENTED}"
    )


# --- migrating off the JSONL -----------------------------------------------


def _write_legacy(path: pathlib.Path) -> None:
    lines = [
        {
            "entry_id": "legacy-1",
            "record": "intent",
            "ts": "2026-10-08T12:00:00+00:00",
            "tool": "apply_keyword_bid",
            "entity_type": "keyword",
            "entity_id": 555,
            "entity_name": "kale",
            "path": "C > A > kale",
            "field": "bid",
            "before": "1.00",
            "after": "1.50",
            "campaign_id": 7,
            "projected_daily_spend_delta": "2.00",
            "reverts_entry_id": None,
            "items": 4,
        },
        {
            "entry_id": "legacy-1",
            "record": "outcome",
            "ts": "2026-10-08T12:00:05+00:00",
            "outcome": "applied",
            "detail": "",
            "observed_after": "1.50",
            "failures": [],
        },
        {
            "entry_id": "legacy-2",
            "record": "intent",
            "ts": "2026-10-08T13:00:00+00:00",
            "tool": "apply_campaign_status",
            "entity_type": "campaign",
            "entity_id": 888,
            "entity_name": "C",
            "path": "C",
            "field": "status",
            "before": "ENABLED",
            "after": "PAUSED",
            "campaign_id": 888,
            "projected_daily_spend_delta": "0",
        },
        "{ this line is corrupt",
    ]
    with path.open("w", encoding="utf-8") as handle:
        for line in lines:
            handle.write((line if isinstance(line, str) else json.dumps(line)) + "\n")


def test_importing_the_jsonl_folds_intent_and_outcome_into_one_row():
    use_temp_db()
    source = pathlib.Path(_TEMP_DIRS[-1].name) / "changes.jsonl"
    _write_legacy(source)

    result = ledger.import_legacy_jsonl(source)
    assert result["read"] == 2, "the corrupt line must be skipped, not fatal"
    assert result["inserted"] == 2

    done = logdb.find_entry("legacy-1")
    assert done["outcome"] == "applied" and done["observed_after"] == "1.50"
    assert done["before"] == "1.00" and done["after"] == "1.50"
    assert done["items"] == 4, "an unknown key must survive in extra_json"

    unfinished = logdb.find_entry("legacy-2")
    assert unfinished["outcome"] is None, "an intent with no outcome stays unfinished"


def test_importing_twice_inserts_nothing_the_second_time():
    use_temp_db()
    source = pathlib.Path(_TEMP_DIRS[-1].name) / "changes.jsonl"
    _write_legacy(source)

    ledger.import_legacy_jsonl(source)
    logdb.complete_change("legacy-2", outcome=logdb.APPLIED, detail="happened after migrating")

    again = ledger.import_legacy_jsonl(source)
    assert again["inserted"] == 0 and again["already_present"] == 2

    entry = logdb.find_entry("legacy-2")
    assert entry["outcome"] == "applied", (
        "a re-run must not roll a row back to its state at migration time"
    )


def test_the_ledger_facade_keeps_the_keys_revert_change_reads():
    use_temp_db()
    entry_id = ledger.new_entry_id()
    ledger.write_intent(
        entry_id,
        tool="apply_keyword_bid",
        entity_type="keyword",
        entity_id=42,
        entity_name="kale",
        path="C > A > kale",
        field="bid",
        before="1.00",
        after="2.00",
        campaign_id=7,
        projected_daily_spend_delta="3.00",
    )
    ledger.write_outcome(entry_id, outcome=ledger.APPLIED, observed_after="2.00")

    entry = ledger.find_entry(entry_id)
    for key in (
        "entry_id", "ts", "tool", "entity_type", "entity_id", "entity_name",
        "path", "field", "before", "after", "outcome", "outcome_detail",
        "reverts_entry_id", "campaign_id", "observed_after", "failures",
    ):
        assert key in entry, f"revert_change and _ledger_entry read {key!r}"
    assert int(entry["entity_id"]) == 42, "revert_change does int(entry['entity_id'])"


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
        except Exception as exc:  # a crash is a failure too, not a traceback
            failures += 1
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    for tmp in _TEMP_DIRS:
        tmp.cleanup()
    print(f"\n{len(tests)} tests.")
    print("All checks passed." if not failures else f"{failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
