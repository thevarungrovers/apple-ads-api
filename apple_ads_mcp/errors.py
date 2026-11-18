"""The one place an exception becomes a message the agent can read.

In mcp 2.3.0, `Tool.run` turns any exception that is not a ToolError into an
opaque "Error executing tool X" and keeps the detail server-side. So an
exception that is not converted tells the agent nothing: it cannot see that the
ad account id is missing, that the ledger could not be written, or that Apple
rate-limited it, and will most likely send the same call again.

`logdb.instrument_tool` -- the wrapper every tool already passes through -- runs
every exception through `to_tool_error`. A ToolError passes through untouched
(its wording was written for the reader); anything else gets a stable prefix:

  CONFIG           .env, the signing key or guardrails.toml is missing or wrong
  LEDGER           the change ledger could not be written. A write REFUSES rather
                   than run unrecorded, so on the intent side nothing was sent
  INVALID_REQUEST  a request model refused its own arguments (pydantic) or a
                   value could not be parsed
  INTERNAL         anything else: a bug. The tool_calls row keeps the detail.

Apple's own failures are classified in `client.describe_api_error`:
AUTH, FORBIDDEN_ROLE, NOT_FOUND, RATE_LIMITED, INVALID, TRANSIENT.
"""

from __future__ import annotations

import sqlite3

from mcp.server.mcpserver.exceptions import ToolError

CONFIG = "CONFIG"
LEDGER = "LEDGER"
INVALID_REQUEST = "INVALID_REQUEST"
INTERNAL = "INTERNAL"

AUTH = "AUTH"
FORBIDDEN_ROLE = "FORBIDDEN_ROLE"
NOT_FOUND = "NOT_FOUND"
RATE_LIMITED = "RATE_LIMITED"
INVALID = "INVALID"
TRANSIENT = "TRANSIENT"

PREFIXES = (
    CONFIG, LEDGER, INVALID_REQUEST, INTERNAL,
    AUTH, FORBIDDEN_ROLE, NOT_FOUND, RATE_LIMITED, INVALID, TRANSIENT,
)


def message_for(exc: BaseException) -> str:
    """The prefixed message for a non-ToolError exception."""
    from apple_ads_platform import ApiException
    from pydantic import ValidationError

    from apple_ads_mcp.config import ConfigError
    from apple_ads_mcp.guardrails import GuardrailConfigError

    if isinstance(exc, ApiException):
        from apple_ads_mcp.client import describe_api_error

        return describe_api_error("the request", exc)
    if isinstance(exc, (ConfigError, GuardrailConfigError)):
        return f"{CONFIG}: {exc}"
    if isinstance(exc, sqlite3.Error):
        return (
            f"{LEDGER}: the local log database could not be written "
            f"({type(exc).__name__}: {exc}). Writes refuse rather than run "
            f"unrecorded. Check logs/apple-ads.db (disk space, permissions, a lock "
            f"held by another process), then run list_my_changes."
        )
    if isinstance(exc, ValidationError):
        return f"{INVALID_REQUEST}: {_short_validation(exc)}"
    if isinstance(exc, (ValueError, TypeError, KeyError, LookupError)):
        text = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
        return f"{INVALID_REQUEST}: {type(exc).__name__}: {text}"
    return f"{INTERNAL}: {type(exc).__name__}: {exc}"


def _short_validation(exc) -> str:
    try:
        errors = exc.errors()
    except Exception:  # noqa: BLE001
        return str(exc)[:400]
    parts = []
    for error in errors[:5]:
        where = ".".join(str(p) for p in error.get("loc", ()))
        parts.append(f"{where or exc.title}: {error.get('msg')}")
    return f"{exc.title} rejected its arguments -- " + "; ".join(parts)


def to_tool_error(exc: BaseException) -> ToolError:
    if isinstance(exc, ToolError):
        return exc
    return ToolError(message_for(exc))
