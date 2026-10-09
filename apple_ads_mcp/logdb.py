"""The SQLite log database: every tool call, every API call, every mutation.

One file, `logs/apple-ads.db`, gitignored because the rows carry real entity
ids. It replaces the `.audit/changes.jsonl` ledger rather than sitting beside
it -- `revert_change` and `reconcile_ledger` read their history from here now.

THREE TABLES, AND THE RELATIONSHIP BETWEEN THEM IS THE POINT:

    tool_calls   one row per MCP tool invocation (or CLI command)
    api_calls    one row per outbound call to Apple, linked to the tool call
                 that caused it
    changes      the mutation ledger: what we meant to change and how it went

A single tool call can fan out to many API calls -- `query_audit` loops over
every entity type, `reconcile_ledger` pulls a thousand rows -- so logging only
at the API layer would give you N unrelated rows and no way to ask "what did
the agent actually do". `api_calls.tool_call_id` is what makes that question
answerable. It is NULL for an API call made outside any tool, which is the
normal case for the standalone fetch scripts.

TWO ERROR POLICIES, DELIBERATELY DIFFERENT:

  - Observability writes (tool_calls, api_calls) NEVER raise. A bug in the
    logger must not turn a working tool into a failure, and the MCP server's
    stderr is invisible in normal use, so a logging error would be both fatal
    and silent. Everything in this file that records an observation is wrapped.

  - Ledger writes (changes) DO raise. Since the JSONL was retired this table is
    the only record of a real-money mutation, and the thing `revert_change`
    reads to undo one. Losing a row quietly is worse than failing the write
    that would have produced it.

TWO-PHASE, EXACTLY AS THE JSONL WAS: `insert_change` writes the intent BEFORE
the API call leaves the process and `complete_change` fills in the outcome
after it returns. A row whose `outcome` is NULL is the dangerous one -- the
write started and nothing recorded how it ended, so it may well have landed at
Apple's end. That is an INSERT followed by an UPDATE, never an upsert: in an
`ON CONFLICT ... DO UPDATE`, `excluded.col` is the value that WOULD have been
inserted, so a clause written to preserve a column can silently overwrite it.
Two statements cannot express that bug.

CONCURRENCY. The MCP server is a long-lived process while `apple_ads_cli.py`
runs in a terminal, so two processes write here at once. WAL plus a busy
timeout is what makes that work; the default journal mode would hand the second
writer `database is locked`. `synchronous=FULL` because the ledger half of this
file is evidence, and the JSONL it replaced was fsync'd per record.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import datetime as dt
import functools
import inspect
import json
import os
import sqlite3
import sys
import threading
import time
import typing
import uuid
from pathlib import Path
from typing import Any, Iterator

from apple_ads_mcp.config import REPO_ROOT

#: Resolved from the package, never from the working directory. The MCP server
#: is spawned by Claude Code with ITS cwd, not the repo's -- a relative path
#: here would scatter one database per directory the agent happened to start
#: in. The same reasoning the stderr logs demonstrate the hard way.
LOG_DIR = REPO_ROOT / "logs"
DB_PATH = LOG_DIR / "apple-ads.db"

SCHEMA_VERSION = 1

#: Outcome vocabulary, re-exported through `ledger` for existing callers.
APPLIED = "applied"
FAILED = "failed"
PARTIAL = "partial"
REFUSED = "refused"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id          INTEGER PRIMARY KEY,
    started_at  TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    pid         INTEGER NOT NULL,
    argv        TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY,
    session_id  INTEGER REFERENCES sessions(id),
    ts          TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    tool_name   TEXT    NOT NULL,
    args_json   TEXT,
    ok          INTEGER,
    error       TEXT,
    duration_ms INTEGER,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS api_calls (
    id           INTEGER PRIMARY KEY,
    tool_call_id INTEGER REFERENCES tool_calls(id),
    session_id   INTEGER REFERENCES sessions(id),
    ts           TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    method       TEXT    NOT NULL,
    path         TEXT,
    request_json TEXT,
    http_status  INTEGER,
    ok           INTEGER,
    error        TEXT,
    duration_ms  INTEGER,
    finished_at  TEXT
);

CREATE TABLE IF NOT EXISTS changes (
    entry_id                    TEXT PRIMARY KEY,
    tool_call_id                INTEGER REFERENCES tool_calls(id),
    ts                          TEXT NOT NULL,
    tool                        TEXT NOT NULL,
    entity_type                 TEXT NOT NULL,
    entity_id                   TEXT,
    entity_name                 TEXT,
    path                        TEXT,
    field                       TEXT,
    before                      TEXT,
    after                       TEXT,
    campaign_id                 INTEGER,
    projected_daily_spend_delta TEXT,
    reverts_entry_id            TEXT,
    extra_json                  TEXT,
    outcome                     TEXT,
    outcome_ts                  TEXT,
    outcome_detail              TEXT,
    observed_after              TEXT,
    failures_json               TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_ts   ON tool_calls(ts);
CREATE INDEX IF NOT EXISTS idx_api_calls_ts    ON api_calls(ts);
CREATE INDEX IF NOT EXISTS idx_api_calls_tool  ON api_calls(tool_call_id);
CREATE INDEX IF NOT EXISTS idx_changes_ts      ON changes(ts);
CREATE INDEX IF NOT EXISTS idx_changes_reverts ON changes(reverts_entry_id);
"""

# One connection per thread. The MCP SDK runs sync tools on a worker thread, and
# a sqlite3 connection may only be used from the thread that created it.
_local = threading.local()
_session_id: int | None = None
_session_lock = threading.Lock()

#: The tool call currently in flight, so an API call can name its parent. A
#: ContextVar rather than a global because anyio copies the context into the
#: worker thread it runs a sync tool on, which a threading.local would not
#: survive and a plain global would get wrong under concurrency.
_current_tool_call: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "apple_ads_current_tool_call", default=None
)


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


#: Request payloads are capped before they are stored. A report request carries
#: whole selector trees and a bulk bid update carries every item; neither is
#: worth unbounded rows, and the first few thousand characters are what anyone
#: reading the log back actually looks at.
MAX_JSON_CHARS = 8000


def _dumps(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = json.dumps(value, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        text = json.dumps(str(value))
    if len(text) > MAX_JSON_CHARS:
        return text[:MAX_JSON_CHARS] + f'... [truncated, {len(text)} chars]'
    return text


def _loads(raw: str | None) -> Any:
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return raw


def connect() -> sqlite3.Connection:
    """The calling thread's connection, opening and migrating it on first use."""
    existing = getattr(_local, "conn", None)
    if existing is not None:
        return existing

    LOG_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(LOG_DIR, 0o700)
    except OSError:
        pass

    fresh = not DB_PATH.exists()
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL lets the CLI write while the server holds a read; busy_timeout is what
    # turns a concurrent writer from an exception into a short wait. FULL
    # because the changes table is the mutation record, not just telemetry.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )
    if fresh:
        try:
            os.chmod(DB_PATH, 0o600)
        except OSError:
            pass

    _local.conn = conn
    return conn


def session_id(source: str = "mcp") -> int:
    """This process's row in `sessions`, created once and reused."""
    global _session_id
    if _session_id is not None:
        return _session_id
    with _session_lock:
        if _session_id is not None:
            return _session_id
        cursor = connect().execute(
            "INSERT INTO sessions(started_at, source, pid, argv) VALUES(?,?,?,?)",
            (_now(), source, os.getpid(), _dumps(sys.argv)),
        )
        _session_id = int(cursor.lastrowid)
    return _session_id


def _source_default() -> str:
    """`mcp` when running under the server, `cli` otherwise.

    argv[0] is the only thing that distinguishes them: the server is launched as
    an absolute path ending in `apple_ads_mcp/server.py`, every other entry
    point is a root script.
    """
    return "mcp" if Path(sys.argv[0]).name == "server.py" else "cli"


# --- observability: never raises ------------------------------------------


@contextlib.contextmanager
def record_tool_call(tool_name: str, args: dict[str, Any] | None = None) -> Iterator[None]:
    """Log one tool invocation, and make it the parent of its API calls.

    Swallows its own failures: see the module docstring. The tool body runs
    whether or not any of this worked.
    """
    source = _source_default()
    started = time.monotonic()
    row_id: int | None = None
    try:
        cursor = connect().execute(
            "INSERT INTO tool_calls(session_id, ts, source, tool_name, args_json) "
            "VALUES(?,?,?,?,?)",
            (session_id(source), _now(), source, tool_name, _dumps(args)),
        )
        row_id = int(cursor.lastrowid)
    except Exception:
        row_id = None

    token = _current_tool_call.set(row_id)
    try:
        yield
    except BaseException as exc:
        _finish_tool_call(row_id, started, ok=False, error=f"{type(exc).__name__}: {exc}")
        raise
    else:
        _finish_tool_call(row_id, started, ok=True, error=None)
    finally:
        _current_tool_call.reset(token)


def _finish_tool_call(row_id: int | None, started: float, *, ok: bool, error: str | None) -> None:
    if row_id is None:
        return
    try:
        connect().execute(
            "UPDATE tool_calls SET ok=?, error=?, duration_ms=?, finished_at=? WHERE id=?",
            (1 if ok else 0, error, int((time.monotonic() - started) * 1000), _now(), row_id),
        )
    except Exception:
        pass


@contextlib.contextmanager
def record_api_call(
    method: str,
    *,
    path: str | None = None,
    request: Any = None,
) -> Iterator[dict[str, Any]]:
    """Log one outbound call to Apple, linked to the tool call that caused it.

    Yields a small dict the caller may stamp `http_status` into -- the SDK path
    only learns the status from an exception, while the break-glass CLI has it
    for every response.
    """
    source = _source_default()
    started = time.monotonic()
    slot: dict[str, Any] = {}
    row_id: int | None = None
    try:
        cursor = connect().execute(
            "INSERT INTO api_calls"
            "(tool_call_id, session_id, ts, source, method, path, request_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                _current_tool_call.get(),
                session_id(source),
                _now(),
                source,
                method,
                path,
                _dumps(request),
            ),
        )
        row_id = int(cursor.lastrowid)
    except Exception:
        row_id = None

    try:
        yield slot
    except BaseException as exc:
        status = slot.get("http_status") or getattr(exc, "status", None)
        _finish_api_call(
            row_id, started, ok=False, error=f"{type(exc).__name__}: {exc}", status=status
        )
        raise
    else:
        _finish_api_call(row_id, started, ok=True, error=None, status=slot.get("http_status"))


def _finish_api_call(
    row_id: int | None,
    started: float,
    *,
    ok: bool,
    error: str | None,
    status: Any = None,
) -> None:
    if row_id is None:
        return
    try:
        connect().execute(
            "UPDATE api_calls SET ok=?, error=?, http_status=?, duration_ms=?, finished_at=? "
            "WHERE id=?",
            (
                1 if ok else 0,
                error,
                int(status) if isinstance(status, int) else None,
                int((time.monotonic() - started) * 1000),
                _now(),
                row_id,
            ),
        )
    except Exception:
        pass


class InstrumentedServer:
    """An `MCPServer` whose `@tool()` decorator also logs the call.

    Wrapping the registrar rather than the 38 tool bodies: one place to change,
    nothing for a new tool to forget, and no decorator to leave off by accident.
    Every other attribute passes straight through to the real server.

    The wrapper copies the wrapped function's signature and annotations
    deliberately -- the MCP SDK derives a tool's input schema from the
    signature and its output schema from the return annotation, so a wrapper
    that lost either would publish a tool taking `*args` and returning nothing.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        register_tool = self._inner.tool(*args, **kwargs)

        def decorate(fn: Any) -> Any:
            return register_tool(instrument_tool(fn))

        return decorate


#: Tools that could not be instrumented, by name. Should always be empty; a
#: test asserts it, because the fallback below is silent by design and a silent
#: gap in the log is exactly the thing worth failing a build over.
UNINSTRUMENTED: list[str] = []


def instrument_tool(fn: Any) -> Any:
    """Wrap one tool function so each invocation writes a `tool_calls` row.

    The annotations are RESOLVED here rather than passed through, and that is
    not a detail. Both tool modules use `from __future__ import annotations`,
    so every annotation is a string that pydantic resolves against the
    function's `__globals__` -- and a wrapper defined in this module has THIS
    module's globals, where `ApplyResult` and `ChangePreview` do not exist. A
    pass-through wrapper therefore registers fine and then fails to build an
    output schema, which the SDK reports as a warning and carries on from. So
    the hints are resolved against the original function and attached as real
    objects, leaving pydantic nothing to look up.
    """
    try:
        hints = typing.get_type_hints(fn, include_extras=True)
        signature = inspect.signature(fn)
        resolved = signature.replace(
            parameters=[
                parameter.replace(annotation=hints.get(parameter.name, parameter.annotation))
                for parameter in signature.parameters.values()
            ],
            return_annotation=hints.get("return", signature.return_annotation),
        )
    except Exception:
        # Better an uninstrumented tool than one whose schema we degraded.
        UNINSTRUMENTED.append(getattr(fn, "__name__", repr(fn)))
        return fn

    def bind(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
        try:
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            return dict(bound.arguments)
        except TypeError:
            return {"args": list(args), "kwargs": kwargs}

    def finish(wrapper: Any) -> Any:
        wrapper.__signature__ = resolved
        wrapper.__annotations__ = hints
        return wrapper

    if asyncio.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            with record_tool_call(fn.__name__, bind(args, kwargs)):
                return await fn(*args, **kwargs)

        return finish(async_wrapper)

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with record_tool_call(fn.__name__, bind(args, kwargs)):
            return fn(*args, **kwargs)

    return finish(wrapper)


# --- the mutation ledger: raises on failure, by design --------------------


def new_entry_id() -> str:
    return uuid.uuid4().hex


def insert_change(
    entry_id: str,
    *,
    tool: str,
    entity_type: str,
    entity_id: Any,
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
    connect().execute(
        "INSERT INTO changes"
        "(entry_id, tool_call_id, ts, tool, entity_type, entity_id, entity_name, path, "
        " field, before, after, campaign_id, projected_daily_spend_delta, "
        " reverts_entry_id, extra_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            entry_id,
            _current_tool_call.get(),
            _now(),
            tool,
            entity_type,
            None if entity_id is None else str(entity_id),
            entity_name,
            path,
            field,
            None if before is None else str(before),
            None if after is None else str(after),
            campaign_id,
            projected_daily_spend_delta,
            reverts_entry_id,
            _dumps(extra) if extra else None,
        ),
    )


def complete_change(
    entry_id: str,
    *,
    outcome: str,
    detail: str = "",
    observed_after: Any = None,
    failures: list[dict[str, Any]] | None = None,
) -> None:
    """Record how it went. A plain UPDATE, never an upsert -- see the docstring."""
    connect().execute(
        "UPDATE changes SET outcome=?, outcome_ts=?, outcome_detail=?, observed_after=?, "
        "failures_json=? WHERE entry_id=?",
        (
            outcome,
            _now(),
            detail,
            None if observed_after is None else str(observed_after),
            _dumps(failures or []),
            entry_id,
        ),
    )


def _row_to_entry(row: sqlite3.Row) -> dict[str, Any]:
    """One `changes` row in the shape the JSONL ledger used to hand back.

    Existing callers read `entry["before"]`, `entry.get("outcome")` and the rest
    straight off this dict, so the key set is a contract, not an internal
    detail. `extra` is spliced back to the top level because that is where the
    JSONL put it.
    """
    entry: dict[str, Any] = {
        "entry_id": row["entry_id"],
        "ts": row["ts"],
        "tool": row["tool"],
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "entity_name": row["entity_name"],
        "path": row["path"],
        "field": row["field"],
        "before": row["before"],
        "after": row["after"],
        "campaign_id": row["campaign_id"],
        "projected_daily_spend_delta": row["projected_daily_spend_delta"],
        "reverts_entry_id": row["reverts_entry_id"],
        "outcome": row["outcome"],
        "outcome_ts": row["outcome_ts"],
        "outcome_detail": row["outcome_detail"],
        "observed_after": row["observed_after"],
        "failures": _loads(row["failures_json"]) or [],
    }
    extra = _loads(row["extra_json"])
    if isinstance(extra, dict):
        entry.update(extra)
    return entry


def entries(since: dt.datetime | None = None) -> list[dict[str, Any]]:
    """Every change, newest first. `outcome` is None for an unfinished write."""
    sql = "SELECT * FROM changes"
    params: tuple[Any, ...] = ()
    if since is not None:
        sql += " WHERE ts >= ?"
        params = (since.isoformat(timespec="seconds"),)
    sql += " ORDER BY ts DESC, rowid DESC"
    return [_row_to_entry(row) for row in connect().execute(sql, params)]


def find_entry(entry_id: str) -> dict[str, Any] | None:
    row = connect().execute("SELECT * FROM changes WHERE entry_id=?", (entry_id,)).fetchone()
    return None if row is None else _row_to_entry(row)


def reverted_entry_ids() -> set[str]:
    """Entries that some later, successful entry already reverted."""
    rows = connect().execute(
        "SELECT DISTINCT reverts_entry_id FROM changes "
        "WHERE reverts_entry_id IS NOT NULL AND outcome=?",
        (APPLIED,),
    )
    return {row[0] for row in rows}
