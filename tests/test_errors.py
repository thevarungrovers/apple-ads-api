#!/usr/bin/env python3
"""Every failure reaches the agent as a prefixed ToolError, never a bare one.

mcp 2.3.0 replaces any exception that is not a ToolError with "Error executing
tool X", so these assert on what `logdb.instrument_tool` -- the wrapper every
tool passes through -- re-raises.

Run it without touching Apple:
    ./venv/bin/python tests/test_errors.py
"""

from __future__ import annotations

import pathlib
import sqlite3
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import KEYWORD, rig, run, temp_db  # noqa: E402

from apple_ads_platform import ApiException, BulkNegativeKeywordCreate  # noqa: E402
from mcp.server.mcpserver.exceptions import ToolError  # noqa: E402

from apple_ads_mcp import client, ledger, logdb  # noqa: E402
from apple_ads_mcp.config import ConfigError  # noqa: E402

APPLE_BODY = (
    '{"error":{"errors":[{"messageCode":"INVALID_ATTRIBUTE_TYPE",'
    '"field":"bid.amount","message":"bid must be at least 0.10"}]}}'
)


def _tool_raising(exc: BaseException):
    def failing_tool() -> str:
        raise exc

    return logdb.instrument_tool(failing_tool)


def _message(exc: BaseException) -> str:
    temp_db()
    try:
        _tool_raising(exc)()
    except ToolError as raised:
        return str(raised)
    raise AssertionError("expected a ToolError")


def test_config_errors_are_prefixed() -> None:
    assert _message(ConfigError("no APPLE_ADS_AD_ACCOUNT_ID")).startswith("CONFIG: ")


def test_ledger_errors_are_prefixed() -> None:
    message = _message(sqlite3.OperationalError("database is locked"))
    assert message.startswith("LEDGER: ") and "database is locked" in message


def test_pydantic_validation_errors_are_prefixed() -> None:
    try:
        BulkNegativeKeywordCreate(text=7, match_type=12)  # type: ignore[arg-type]
    except Exception as exc:  # noqa: BLE001
        validation = exc
    else:
        raise AssertionError("the SDK accepted nonsense")
    assert _message(validation).startswith("INVALID_REQUEST: ")


def test_anything_else_is_internal_and_named() -> None:
    message = _message(ZeroDivisionError("boom"))
    assert message == "INTERNAL: ZeroDivisionError: boom", message


def test_a_tool_error_passes_through_untouched() -> None:
    assert _message(ToolError("keyword 1 is already PAUSED")) == "keyword 1 is already PAUSED"


def test_api_statuses_map_to_their_class() -> None:
    expected = {429: "RATE_LIMITED", 500: "TRANSIENT", 400: "INVALID", 403: "FORBIDDEN_ROLE",
                401: "AUTH", 404: "NOT_FOUND"}
    for status, prefix in expected.items():
        message = _message(ApiException(status=status, reason="r", body=APPLE_BODY))
        assert message.startswith(f"{prefix}: "), (status, message)


def test_apple_error_bodies_are_parsed_not_dumped() -> None:
    message = client.describe_api_error(
        "keywords_id_put", ApiException(status=400, reason="Bad Request", body=APPLE_BODY)
    )
    assert "INVALID_ATTRIBUTE_TYPE bid.amount: bid must be at least 0.10" in message, message
    assert '{"error"' not in message


def test_the_failed_call_is_logged_with_its_prefix() -> None:
    temp_db()
    try:
        _tool_raising(ConfigError("missing"))()
    except ToolError:
        pass
    row = logdb.connect().execute("SELECT ok, error FROM tool_calls").fetchone()
    assert row["ok"] == 0 and "CONFIG: missing" in row["error"], dict(row)


def test_a_ledger_failure_before_the_request_sends_nothing() -> None:
    tools, apple, _ = rig()
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.10")
    original = ledger.write_intent

    def broken(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    ledger.write_intent = broken
    try:
        try:
            tools["apply_keyword_bid"](preview_token=preview.preview_token)
        except ToolError as exc:
            assert str(exc).startswith("LEDGER: ") and "NOTHING WAS SENT" in str(exc), exc
        else:
            raise AssertionError("an unrecorded write went ahead")
    finally:
        ledger.write_intent = original
    assert apple.writes() == []


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
