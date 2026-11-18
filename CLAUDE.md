# CLAUDE.md

Guidance for agents working on this repository: the `apple-ads` MCP server
(`apple_ads_mcp/`), which lets an AI agent read a LIVE Apple Ads (Search Ads)
account and change it through guarded preview/apply pairs, on the Apple Ads
Platform API (`apple-ads-platform==1.109.0`, pinned).

## Running things

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt -r requirements-dev.txt
./venv/bin/pytest                               # the whole suite: offline, nothing sent
./venv/bin/python tests/test_tool_surface.py    # any test file also runs as a script
./venv/bin/python apple_ads_mcp/server.py       # the MCP server (stdio)
```

- The suite is green at every commit. It never reaches Apple: `tests/kindkit.py`
  replaces `client.call` with a fake account built from real SDK models and makes
  building a live client raise, and `tests/conftest.py` points the log database
  at a temp directory.
- `test_platform_connection.py` at the root is a LIVE connection check, not a
  unit test; `pytest.ini` keeps it out of collection.

## Rules that are not style

- **There is no sandbox, and the account is live.** Never call an `apply_*`
  tool, `revert_change`, or `apple_ads_cli.py --apply` to "check something".
  Reads and previews only.
- **Money is a decimal string in major units** wherever an agent or a human sees
  it: `"1.20"`, never cents, never a float. `client.parse_decimal` and
  `client.to_money` are the edges.
- **Nothing is deleted.** No `*_delete` method is reachable. Created things
  start PAUSED (negative keywords excepted: a paused negative does nothing), and
  pausing is the undo -- reverting a creation pauses it.
- **HTTP 200 does not mean applied.** Every write is judged by Apple's read-back
  (`client.landed`); a kind's `observe` must compare what Apple echoed with what
  was asked.
- **`FORBIDDEN_METHODS` in `tests/test_tool_surface.py` is the one explicit list
  of SDK methods that must stay unreachable.** The test scans every string in
  `apple_ads_mcp/`. A method comes off the list only in the commit that adds the
  kind that calls it.
- **Every tool declares all four ToolAnnotations hints**, and the tool set is
  pinned in `tests/test_tool_surface.py` and in `install.py` (`SAFE_TOOLS` /
  `WRITE_TOOLS`). Adding a tool means editing both. Never allowlist an apply.
- **Failures reach the agent.** `logdb.instrument_tool` maps every non-ToolError
  through `apple_ads_mcp/errors.py` (CONFIG / LEDGER / INVALID_REQUEST /
  INTERNAL); Apple's errors are classified in `client.describe_api_error` (AUTH,
  FORBIDDEN_ROLE, NOT_FOUND, RATE_LIMITED, INVALID, TRANSIENT). Reads retry;
  writes never do.
- **Day boundaries are LOCAL time.** The ledger stores UTC ISO (`logdb._now()`);
  the daily cap (`guardrails.DailyUsage`) converts local midnight to UTC and
  compares parsed instants.
- **Enables are counted against the daily total, not the per-change limit.**
  Anything that resumes or widens delivery sets `Proposal.is_enable` and projects
  the most it could add (`common.campaign_headroom`, `campaign_enable_bound`).
- **The break-glass CLI is for humans.** `apple_ads_cli.py --apply` needs a TTY
  and a typed confirmation, honours the kill switch and writes a ledger row.
  Never tell an agent to run it.
- **Names the agent creates are `<OWNER>-<agent>-<base>`** (`apple_ads_mcp/
  naming.py`). The identity is `APPLE_ADS_OWNER_INITIALS` / `APPLE_ADS_AGENT_NAME`
  in this checkout's `.env` only -- never a tool argument, never the process env
  -- so one checkout is one identity. An empty part is skipped; a malformed one
  blocks creation naming its key. A creating kind stamps through
  `ctx.name_for_create`, reports `Proposal.created_names` and declares
  `creates_named`; the engine fails `name_prefix` otherwise. Renames go through
  `ctx.name_for_rename` and reach only the agent's own entities. Over 200
  characters is refused, never truncated. Tests and docs use the placeholders
  `AB` / `max` (and `CD` / `bot`); real values never appear in the repo.
- **This repository is public.** No company, brand, app, campaign, ad group or
  keyword names from a real account, no real ids (org, ad account, campaign,
  adamId), no emails, no credentials. Fixtures use placeholders (`Example App`,
  `1234567890`, `example.com`, `Springfield`). Real ids live in `.env`. Never
  commit `.env`, `*.pem`, `logs/`, `reports/`, `.audit/` or `.claude/plans/`.

## Adding a change kind

A write is a `ChangeKind` (`apple_ads_mcp/changes/base.py`). The engine
(`changes/engine.py`) runs the one sequence for all of them: the token, the kill
switch, the session and daily caps, staleness, one two-phase ledger row per item,
the read-back, and revert.

1. Subclass `ChangeKind` in `apple_ads_mcp/changes/` (or `StatusKind` in
   `common.py` for an ENABLED/PAUSED flip):
   - `name` -> `preview_<name>` / `apply_<name>`; titles; `apply_annotations`
     (`APPLY`, or `APPLY_DESTRUCTIVE` for campaign-scale or creating changes);
     `cost` (3 for a creation); `bulk=True` for a batch; `ledger_keys` = the
     (entity_type, field) pairs its rows use -- how revert finds it.
   - `propose(self, ctx, ...)` -- its parameters ARE the preview tool's schema;
     annotate each with `Annotated[..., Field(description=...)]`; the docstring
     is the tool description. Return a `Proposal`: JSON-able `items` (build them
     with `base.item(...)`), `checks`, `projected`, `is_enable`, and the readable
     fields (or `children` for a batch).
   - `current(ctx, item)` -- today's value, spelled exactly like `item["before"]`
     (None for a creation).
   - `request(ctx, items)` -> `(sdk_method, kwargs)` and `observe(ctx, response,
     items)` -> one `Outcome` per item. Override `execute` if the change takes
     more than one request.
   - `inverse(ctx, entry)` -> `(kind, item)` and, if undoing it restores spend,
     `revert_projection`. A creation's inverse is `StatusKind.undo_creation`.
2. Add it to `changes/__init__.all_kinds()`, pin both tool names in
   `tests/test_tool_surface.py` and `install.py`, take its SDK method off
   `FORBIDDEN_METHODS` in the same commit, and mention it in
   `server.INSTRUCTIONS`, `.claude/skills/apple-ads/SKILL.md` and the README.
3. Test it with `tests/kindkit.py` (`rig()` returns the tools, a `FakeApple` and
   the state): the preview renders before/after; a FAIL blocks the apply; a stale
   value is refused; the inverse round-trips or is refused with its reason.
