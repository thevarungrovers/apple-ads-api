#!/usr/bin/env python3
"""The `apple-ads` MCP server: stdio, read tools plus guarded write tools.

Run it the way ~/.claude.json does -- absolute interpreter, absolute script:

    /Users/you/dev/apple-ads-api/venv/bin/python \\
        /Users/you/dev/apple-ads-api/apple_ads_mcp/server.py

Never a bare `python3`, and not `-m`: the launching process's PATH and cwd are
not yours.

The server name matters more than it looks. Claude Code renders a permission
prompt as `apple-ads - apply_campaign_daily_budget`, and its permission rules key
on the TOOL NAME -- which is the whole reason previews and applies are separate
tools rather than one tool with a `dry_run` flag. You can permanently allowlist
every `preview_*` and never allowlist a single `apply_*`; with a flag, one
"always allow" clicked during a harmless preview would silently authorise every
future real write.

This module does NOT call `_bootstrap.ensure_venv()`. See apple_ads_mcp/__init__.
"""

from __future__ import annotations

import pathlib
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

# Launched by absolute path, sys.path[0] is this package's directory, so the
# package itself is not importable until the repo root is on the path.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from apple_ads_mcp import __version__, tools_read, tools_write  # noqa: E402
from apple_ads_mcp.guardrails import Limits, SessionCounters, load_limits  # noqa: E402
from apple_ads_mcp.previews import PreviewStore  # noqa: E402

SERVER_NAME = "apple-ads"

INSTRUCTIONS = """\
Apple Ads, live production account. Read freely; write only through the
preview/apply pair below.

THERE IS NO SANDBOX. Apple runs no test environment for campaign management, so
there is no staging account and no dry-run mode at Apple's end. Every write goes
to the real account, where campaigns are serving and spending roughly 100/day
right now. A mistake here costs money today, not at the next deploy.

HOW TO CHANGE SOMETHING
  1. Read first. get_keyword / list_keywords / keyword_report tell you what the
     current value is and what it has been doing.
  2. Call the matching preview_* tool. It returns a readable before/after, the
     campaign and ad group the entity sits in, whether that campaign is serving
     today, and an upper bound on the daily spend change.
  3. Show the human the preview, then call the apply_* tool with the
     preview_token it returned. The human approves at that prompt -- that
     approval IS the authorization, so never call apply_* without having just
     shown them what it does.

A preview_token is single-use, expires in 10 minutes, and is refused if the
entity's current value has moved since the preview was taken.

MONEY IS ALWAYS A DECIMAL STRING in major units: "1.20" means one dollar twenty.
Never pass cents as an integer -- "120" would be a hundredfold error, and the
argument is rendered verbatim in the prompt a human is approving.

WHAT IS NOT HERE, deliberately: creating or deleting campaigns, ad groups, ads,
creatives and assets; deleting negative keywords (pause them instead -- it is
reversible and achieves the same thing); applying Apple's budget
recommendations in one call. There is no generic request/passthrough tool, so if
an operation is not listed it cannot be performed here. Use the break-glass CLI,
`apple_ads_client.py ... --apply --confirm-live`, and tell the human you are
doing so.

Before proposing anything, get_guardrails tells you the bounds and how much of
this session's change budget is left. list_my_changes shows what you have
already done; revert_change undoes one by its entry_id.
"""


@dataclass
class ServerState:
    """Everything the tools share, built once per server process.

    Created by the lifespan and also parked in a module global, so a tool can
    reach it without every signature growing a `ctx` parameter it would only use
    to find this object.
    """

    limits: Limits
    counters: SessionCounters
    previews: PreviewStore


def build_state() -> ServerState:
    limits = load_limits()
    return ServerState(
        limits=limits,
        counters=SessionCounters(limits),
        previews=PreviewStore(),
    )


# Built once at import, and the lifespan hands out THIS object rather than
# building a second one: two SessionCounters would mean two sets of counters and
# a session budget silently twice what it claims to be.
STATE = build_state()


@asynccontextmanager
async def lifespan(_server: MCPServer) -> AsyncIterator[ServerState]:
    yield STATE


mcp = MCPServer(
    name=SERVER_NAME,
    version=__version__,
    instructions=INSTRUCTIONS,
    lifespan=lifespan,
)

tools_read.register(mcp, STATE)
tools_write.register(mcp, STATE)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
