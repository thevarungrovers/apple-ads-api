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

from apple_ads_mcp import __version__, logdb, tools_lookup, tools_read, tools_write  # noqa: E402
from apple_ads_mcp.guardrails import (  # noqa: E402
    Limits,
    SessionCounters,
    limit_warnings,
    load_limits,
)
from apple_ads_mcp.previews import PreviewStore  # noqa: E402
from apple_ads_mcp.naming import Identity, process_env_warnings  # noqa: E402

SERVER_NAME = "apple-ads"

INSTRUCTIONS = """\
Apple Ads, live production account. Read freely; write only through the
preview/apply pairs.

THERE IS NO SANDBOX. Apple runs no test environment for campaign management and
no validate-only mode, so a preview checks this server's bounds, not whether
Apple will accept the change. Every write goes to the real account, where
campaigns are serving and spending today.

HOW TO CHANGE SOMETHING
  1. Read first. get_account (timezone, currency, your role), get_guardrails
     (bounds, and what is left this session and TODAY), then the get_/list_/
     *_report tools for the current value and what it has been doing.
  2. Call the matching preview_* tool. It returns a readable before/after, where
     the entity sits, whether it is serving today, and an upper bound on the
     daily spend change.
  3. Show the human the preview, then call the apply_* tool with the
     preview_token it returned. The human approves at that prompt -- that
     approval IS the authorization, so never call apply_* without having just
     shown them what it does.

A preview_token is single-use, expires in 10 minutes, belongs to one apply_*
tool, and is refused if the entity's current value has moved since the preview.

MONEY IS ALWAYS A DECIMAL STRING in major units: "1.20" means one dollar twenty.
Never pass cents as an integer -- "120" would be a hundredfold error, and the
argument is rendered verbatim in the prompt a human is approving.

WHAT YOU CAN CHANGE (preview_<name> / apply_<name>):
  bids and budgets   keyword_bid, keyword_bids_bulk, ad_group_default_bid,
                     campaign_daily_budget
  status             keyword_status, ad_group_status, campaign_status, ad_status,
                     negative_keyword_pause, negative_keyword_status
  keywords           add_keywords (created PAUSED), keyword_match_type,
                     negative_keywords_add (created ENABLED)
  bidding            ad_group_bid_strategy, campaign_bid_strategy,
                     ad_group_cpa_goal, campaign_schedule
  targeting          ad_group_search_match, ad_group_demographics,
                     ad_group_devices, ad_group_daypart, ad_group_locations,
                     ad_group_app_downloaders, campaign_countries
  creation           create_campaign, create_ad_group, create_creative,
                     create_ad -- always created PAUSED, 3 applies each
  recommendations    budget_recommendation, target_cpa_recommendation (Apple's
                     advice, through the normal budget / CPA bounds)
  low risk           low_risk_change: renames, and dismissing a recommendation
revert_change undoes one entry (or a batch, by its id); list_my_changes shows
what you have done. An entry recorded 'unknown' (the request timed out) may
have landed: settle it with resolve_unknown_change, which reads Apple and writes
only the ledger -- do not call revert_change just to find out. It records
'failed' only once the entry is 5 minutes old ("too early to tell" says when to
try again) and never after a later change to the same field.

NOTHING IS EVER DELETED. Created things start PAUSED, and pausing is the undo:
reverting a creation pauses it.

THE CAPS. Per change (max_bid, max_daily_budget, % limits, CPA goal limits,
max_projected_delta_per_day), per session (applies and projected spend, reset by
a restart), and PER DAY (max_applies_per_day, max_projected_delta_per_day_total:
read from the ledger, shared by every session and the CLI, reset at LOCAL
midnight). Anything that resumes or widens delivery -- an enable, pausing a
negative, Search Match on, looser targeting, an automated bid strategy, undoing
a pause -- is projected at the most it could add, skips the per-change limit
(SKIP) and counts in full against the session and daily totals. A batch counts
once; a creation counts 3.

ERRORS start with a stable prefix: AUTH, FORBIDDEN_ROLE, NOT_FOUND, RATE_LIMITED,
INVALID, TRANSIENT (from Apple), CONFIG, LEDGER, INVALID_REQUEST, INTERNAL
(local). Reads are retried; writes never are. Read `applied` on every result:
HTTP 200 does not mean the change landed.

WHAT IS NOT HERE, deliberately: deleting anything; shared budget, billing and
account writes; location groups and Maps ads; asset uploads; one-call "apply
Apple's recommendations". There is no generic request/passthrough tool, so if an
operation is not listed it cannot be performed here. Say so, and if the
human wants it done anyway, hand THEM the break-glass command to run in their
own terminal -- never run `apple_ads_cli.py ... --apply` yourself: it needs an
interactive terminal and a typed confirmation, and it is denied to agents.

NAMING. Give create_campaign, create_ad_group, create_creative and create_ad
the BASE name only: the server adds this checkout's prefix from .env
(<OWNER>-<agent>-, e.g. AB-max-spring_promo; one part if only one is set, none
if neither is) and the preview shows the exact final name. A name that reads
as anyone else's prefix is always refused, even with no identity configured;
ours twice is refused; a name over 200 characters is refused, not truncated.
Renames through low_risk_change keep the prefix and reach only your own
entities (your whole prefix, or the ledger shows you created it); hand-made and
other agents' entities are refused. The list_* tools take
origin / agent_name / owner / mine to find what you made; get_guardrails shows
the identity.

READ-ONLY INSTALLS. If get_guardrails reports read_only_server: true, this
process was started with APPLE_ADS_READ_ONLY set and published no apply_* tool
and no revert_change at all -- they are absent from the tool list, not refused
at call time. The preview_* tools still work, so the useful thing to do is show
the human exactly what you would change and let them make it elsewhere.
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
    #: Who this checkout's creations are named after (apple_ads_mcp/naming.py),
    #: read once from .env. Never from a tool argument or the process env.
    identity: Identity = Identity()


def build_state() -> ServerState:
    limits = load_limits()
    # stderr is the MCP client's log, never the protocol stream (stdout).
    for warning in limit_warnings(limits):
        print(f"apple-ads: guardrails warning: {warning}", file=sys.stderr)
    identity = Identity.load()
    for problem in identity.problems:
        print(f"apple-ads: naming error: {problem}", file=sys.stderr)
    for warning in process_env_warnings(identity):
        print(f"apple-ads: naming warning: {warning}", file=sys.stderr)
    return ServerState(
        limits=limits,
        counters=SessionCounters(limits),
        previews=PreviewStore(),
        identity=identity,
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

# Registered through the instrumented wrapper so every tool call lands in
# logs/apple-ads.db. `mcp` itself is what gets run; the wrapper only intercepts
# the decorator, so the server object the SDK sees is unchanged.
_registrar = logdb.InstrumentedServer(mcp)
tools_read.register(_registrar, STATE)
tools_lookup.register(_registrar, STATE)
tools_write.register(_registrar, STATE)


def main() -> None:
    mcp.run("stdio")


if __name__ == "__main__":
    main()
