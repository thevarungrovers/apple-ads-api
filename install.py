#!/usr/bin/env python3
"""Add the `apple-ads` MCP server to Claude Code, Claude Desktop, Cursor or Codex.

Run it and answer two questions:

    python3 install.py

STDLIB ONLY, and `python3` rather than `./venv/bin/python`, deliberately. This
is the first thing someone runs, quite possibly before they have made a venv at
all, so it must not need one. (It still CHECKS for one, because the config it
writes points at `venv/bin/python` and a registration pointing at a missing
interpreter fails silently inside the client, which is a miserable thing to
debug.)

What it writes, per client:

    claude-code      ~/.claude.json                         -> mcpServers
                     ~/.claude/settings.json                -> permissions.allow
    claude-desktop   <app support>/claude_desktop_config.json -> mcpServers
    cursor           ~/.cursor/mcp.json                     -> mcpServers
    codex            ~/.codex/config.toml                   -> [mcp_servers.apple-ads]

Every file is backed up before it is touched, every edit is a merge rather than
an overwrite, and running it twice is a no-op rather than a duplicate.

Non-interactive, for a script or a second machine:

    python3 install.py --agent cursor --read-only
    python3 install.py --agent claude-code --no-read-only --yes
    python3 install.py --agent codex --dry-run
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import platform
import re
import shutil
import sys

REPO_ROOT = pathlib.Path(__file__).resolve().parent
SERVER_NAME = "apple-ads"
SERVER_SCRIPT = REPO_ROOT / "apple_ads_mcp" / "server.py"
VENV_PYTHON = REPO_ROOT / "venv" / "bin" / "python"
WINDOWS_VENV_PYTHON = REPO_ROOT / "venv" / "Scripts" / "python.exe"
SKILL_FILE = REPO_ROOT / ".claude" / "skills" / SERVER_NAME / "SKILL.md"

#: The tools that cannot change anything: 19 reads and 9 previews. A preview
#: calls Apple and mints a token in memory; it writes nothing to the account.
#: Hardcoded because this script must not import the server (that would need the
#: dependencies, which is exactly what it may be running before). Kept honest by
#: `test_installer_tool_list_matches_the_server` in tests/test_tool_surface.py,
#: which compares this list against what actually registers.
SAFE_TOOLS = (
    "whoami",
    "list_campaigns",
    "get_campaign",
    "list_ad_groups",
    "get_ad_group",
    "list_keywords",
    "get_keyword",
    "list_negative_keywords",
    "list_shared_budgets",
    "campaign_report",
    "ad_group_report",
    "keyword_report",
    "search_term_report",
    "keyword_suggestions",
    "budget_recommendations",
    "list_recent_changes",
    "get_guardrails",
    "list_my_changes",
    "reconcile_ledger",
    "preview_keyword_bid",
    "preview_keyword_bids_bulk",
    "preview_keyword_status",
    "preview_ad_group_status",
    "preview_ad_group_default_bid",
    "preview_campaign_status",
    "preview_campaign_daily_budget",
    "preview_negative_keywords_add",
    "preview_negative_keyword_pause",
)

#: The tools that move real money. Never allowlisted, by any answer to anything.
WRITE_TOOLS = (
    "apply_keyword_bid",
    "apply_keyword_bids_bulk",
    "apply_keyword_status",
    "apply_ad_group_status",
    "apply_ad_group_default_bid",
    "apply_campaign_status",
    "apply_campaign_daily_budget",
    "apply_negative_keywords_add",
    "apply_negative_keyword_pause",
    "revert_change",
)

READ_ONLY_ENV = "APPLE_ADS_READ_ONLY"

AGENTS = {
    "1": "claude-code",
    "2": "claude-desktop",
    "3": "cursor",
    "4": "codex",
}


class InstallError(RuntimeError):
    """Something the person running this can fix. Printed without a traceback."""


# --- where each client keeps its config --------------------------------------


def claude_code_config() -> pathlib.Path:
    return pathlib.Path.home() / ".claude.json"


def claude_code_settings() -> pathlib.Path:
    return pathlib.Path.home() / ".claude" / "settings.json"


def claude_desktop_config() -> pathlib.Path:
    """Three different places, one per OS. Claude Desktop does not look elsewhere."""
    home = pathlib.Path.home()
    system = platform.system()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        appdata = os.environ.get("APPDATA")
        base = pathlib.Path(appdata) if appdata else home / "AppData" / "Roaming"
        return base / "Claude" / "claude_desktop_config.json"
    return home / ".config" / "Claude" / "claude_desktop_config.json"


def cursor_config() -> pathlib.Path:
    return pathlib.Path.home() / ".cursor" / "mcp.json"


def codex_config() -> pathlib.Path:
    return pathlib.Path.home() / ".codex" / "config.toml"


# --- small file helpers -------------------------------------------------------


def interpreter() -> pathlib.Path:
    """The venv interpreter the registration will point at.

    Absolute, always. The client launches this with its own PATH and its own
    working directory, neither of which is yours, so a bare `python3` or a
    relative path is a server that works in your terminal and nowhere else.
    """
    if platform.system() == "Windows" and WINDOWS_VENV_PYTHON.exists():
        return WINDOWS_VENV_PYTHON
    return VENV_PYTHON


def preflight() -> list[str]:
    """Problems worth naming before writing anything. Warnings, not refusals."""
    warnings = []
    if not SERVER_SCRIPT.exists():
        raise InstallError(
            f"{SERVER_SCRIPT} does not exist. Run this from a full checkout of the "
            f"apple-ads-api repository."
        )
    python = interpreter()
    if not python.exists():
        warnings.append(
            f"No interpreter at {python}. The registration points there, so the "
            f"client will start the server and get nothing. Create it first:\n"
            f"      python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
        )
    if not (REPO_ROOT / ".env").exists():
        warnings.append(
            "No .env yet, so the server will start and every tool will fail with a "
            "ConfigError. Set it up with:\n"
            "      cp .env.example .env   (then fill it in)\n"
            "      ./venv/bin/python test_platform_connection.py"
        )
    return warnings


def backup(path: pathlib.Path) -> pathlib.Path | None:
    """Copy a file aside before editing it. Returns the copy, or None if new.

    These are other people's config files -- ~/.claude.json also holds every
    other MCP server and a pile of UI state -- so a bug here costs somebody
    their setup, not just this server's.
    """
    if not path.exists():
        return None
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    copy = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, copy)
    return copy


def load_json(path: pathlib.Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise InstallError(
            f"{path} is not valid JSON ({exc}). Fix or move it; this script will "
            f"not overwrite a file it cannot parse."
        ) from exc
    if not isinstance(data, dict):
        raise InstallError(f"{path} holds a {type(data).__name__}, not a JSON object.")
    return data


def write_json(path: pathlib.Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# --- the server block ---------------------------------------------------------


def server_block(read_only: bool) -> dict:
    block: dict = {
        "command": str(interpreter()),
        "args": [str(SERVER_SCRIPT)],
    }
    if read_only:
        # Credentials never go in here -- they stay in .env. This env block
        # carries one flag and nothing else, and the flag is per client, which
        # is the whole point: one checkout, read-only here, writable there.
        block["env"] = {READ_ONLY_ENV: "1"}
    return block


def describe(block: dict) -> str:
    return json.dumps({SERVER_NAME: block}, indent=2)


# --- per-client installers ----------------------------------------------------


def install_json_client(
    path: pathlib.Path, read_only: bool, dry_run: bool, label: str
) -> list[str]:
    """Claude Code, Claude Desktop and Cursor all keep `mcpServers` in JSON."""
    data = load_json(path)
    servers = data.get("mcpServers")
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise InstallError(f"{path}: 'mcpServers' is not an object; refusing to touch it.")

    block = server_block(read_only)
    existing = servers.get(SERVER_NAME)
    notes = []
    if existing == block:
        notes.append(f"{label}: already registered exactly like this -- nothing to do.")
        return notes
    if existing is not None:
        notes.append(f"{label}: replacing the existing '{SERVER_NAME}' entry.")

    servers[SERVER_NAME] = block
    data["mcpServers"] = servers

    if dry_run:
        notes.append(f"{label}: WOULD write {path}")
        return notes

    saved = backup(path)
    write_json(path, data)
    notes.append(f"{label}: wrote {path}" + (f"  (backup: {saved.name})" if saved else "  (new file)"))
    return notes


def install_claude_code_permissions(dry_run: bool) -> list[str]:
    """Pre-approve the 28 tools that cannot change anything.

    Claude Code is the ONLY one of the four with a per-tool allowlist, so it is
    the only one where "approve the safe tools, keep asking about the rest" is
    something the client can be told. The ten write tools are never added here,
    under any answer: not being on this list is what makes them stop and ask.

    A blanket `mcp__apple-ads` entry would approve the server including its
    writes, and `mcp__apple-ads__preview_*` is a wildcard whose support varies
    by version and whose failure is silent. Both are avoided by listing names.
    """
    path = claude_code_settings()
    data = load_json(path)
    permissions = data.get("permissions")
    if permissions is None:
        permissions = {}
    if not isinstance(permissions, dict):
        raise InstallError(f"{path}: 'permissions' is not an object; refusing to touch it.")
    allow = permissions.get("allow")
    if allow is None:
        allow = []
    if not isinstance(allow, list):
        raise InstallError(f"{path}: 'permissions.allow' is not a list; refusing to touch it.")

    wanted = [f"mcp__{SERVER_NAME}__{tool}" for tool in SAFE_TOOLS]
    already = set(allow)
    added = [rule for rule in wanted if rule not in already]
    if not added:
        return [f"permissions: all {len(wanted)} safe tools were already allowed."]

    if dry_run:
        return [f"permissions: WOULD add {len(added)} rules to {path}"]

    allow.extend(added)
    permissions["allow"] = allow
    data["permissions"] = permissions
    saved = backup(path)
    write_json(path, data)
    return [
        f"permissions: added {len(added)} rules to {path}"
        + (f"  (backup: {saved.name})" if saved else "  (new file)"),
        f"            {len(SAFE_TOOLS)} safe tools now run without prompting; the "
        f"{len(WRITE_TOOLS)} that change the account still stop and ask.",
    ]


#: Our table's header line, in either spelling TOML allows for a hyphenated key.
CODEX_HEADER_RE = re.compile(
    rf'^\[\s*mcp_servers\s*\.\s*(?:"{re.escape(SERVER_NAME)}"|{re.escape(SERVER_NAME)})\s*\]\s*$'
)

#: A table header starts a new table and therefore ENDS ours. Anchored at column
#: zero deliberately: `args = ["..."]` contains a `[` but does not start with
#: one, and a regex that stopped at the first `[` anywhere would cut our table in
#: half -- leaving `["/old/server.py"]` behind as a line TOML then reads as a
#: table header of its own, silently inventing a table and swallowing whatever
#: followed it. A continuation line inside a multi-line array is indented, so
#: column zero tells the two apart.
CODEX_TABLE_START_RE = re.compile(r"^\[")


def replace_codex_block(text: str, block: str) -> tuple[str, bool]:
    """Swap our `[mcp_servers."apple-ads"]` table for `block`, or append it.

    Returns (new text, whether an existing table was replaced). Line-based
    rather than one regex: a TOML table runs from its header to the next header,
    and that is a statement about LINES, not about characters.
    """
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    index = 0
    replaced = False

    while index < len(lines):
        if not CODEX_HEADER_RE.match(lines[index].rstrip("\n")):
            out.append(lines[index])
            index += 1
            continue
        replaced = True
        index += 1
        while index < len(lines) and not CODEX_TABLE_START_RE.match(lines[index]):
            index += 1
        out.append(block)
        if index < len(lines):
            out.append("\n")  # keep a blank line before the next table

    if replaced:
        return "".join(out), True

    separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
    if text.endswith("\n\n"):
        separator = ""
    return text + separator + block, False


def codex_block(read_only: bool) -> str:
    """The TOML Codex wants. Hand-built: the stdlib reads TOML but cannot write it.

    The table name is QUOTED. `apple-ads` contains a hyphen, which is legal in a
    TOML bare key but reads as ambiguous, and quoting removes the question.
    """
    args = ", ".join(json.dumps(str(a)) for a in [SERVER_SCRIPT])
    lines = [
        f'[mcp_servers."{SERVER_NAME}"]',
        f"command = {json.dumps(str(interpreter()))}",
        f"args = [{args}]",
    ]
    if read_only:
        lines.append(f'env = {{ {READ_ONLY_ENV} = "1" }}')
    return "\n".join(lines) + "\n"


def install_codex(read_only: bool, dry_run: bool) -> list[str]:
    """Codex keeps MCP servers in TOML, so this is a text merge, not a JSON one.

    Replaces an existing `[mcp_servers."apple-ads"]` table in place and appends
    when there is none, rather than rewriting the file: config.toml holds every
    other Codex setting, and round-tripping it through a parser that cannot
    write comments would lose them.
    """
    path = codex_config()
    block = codex_block(read_only)
    text = path.read_text(encoding="utf-8") if path.exists() else ""

    notes = []
    updated, replaced = replace_codex_block(text, block)
    if replaced:
        notes.append(f'codex: replacing the existing [mcp_servers."{SERVER_NAME}"] table.')

    if updated == text:
        return ["codex: already registered exactly like this -- nothing to do."]
    if dry_run:
        notes.append(f"codex: WOULD write {path}")
        return notes

    saved = backup(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    notes.append(f"codex: wrote {path}" + (f"  (backup: {saved.name})" if saved else "  (new file)"))
    return notes


# --- asking ------------------------------------------------------------------


def ask_agent() -> str:
    print("Which AI agent do you want to add the Apple Ads MCP server to?\n")
    print("  1) Claude Code")
    print("  2) Claude Desktop")
    print("  3) Cursor")
    print("  4) Codex\n")
    while True:
        choice = input("Choose 1-4: ").strip()
        if choice in AGENTS:
            return AGENTS[choice]
        by_name = {name: name for name in AGENTS.values()}
        if choice.lower() in by_name:
            return by_name[choice.lower()]
        print("  Type 1, 2, 3 or 4.")


def ask_read_only(agent: str) -> bool:
    print()
    print("Add the basic permissions only -- the tools that can READ the account,")
    print("but not the ones that can actually change it?")
    print()
    if agent == "claude-code":
        print(f"  Y  {len(SAFE_TOOLS)} read and preview tools are pre-approved and run without")
        print(f"     prompting. The {len(WRITE_TOOLS)} tools that change the account are NOT")
        print("     pre-approved, so each one stops and asks you first.")
        print("  N  Nothing is pre-approved. Every tool asks, including the reads.")
    else:
        print("     (This client has no per-tool permission list, so the limit is")
        print("     applied in the server itself.)")
        print()
        print(f"  Y  The server publishes only the {len(SAFE_TOOLS)} read and preview tools.")
        print(f"     The {len(WRITE_TOOLS)} that change the account are not published at all,")
        print("     so this agent cannot call them.")
        print("  N  All 38 tools are published. Writes still need a preview token.")
    print()
    while True:
        answer = input("Basic permissions only? [Y/n]: ").strip().lower()
        if answer in ("", "y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("  Type Y or N.")


# --- main --------------------------------------------------------------------


def install(agent: str, read_only: bool, dry_run: bool) -> list[str]:
    notes: list[str] = []
    if agent == "claude-code":
        # Claude Code gets the allowlist rather than the server flag: it can
        # express "ask me about writes", which is strictly more useful than not
        # publishing them, and leaves the preview/apply pair intact.
        notes += install_json_client(claude_code_config(), False, dry_run, "claude-code")
        if read_only:
            notes += install_claude_code_permissions(dry_run)
        else:
            notes.append(
                "permissions: nothing pre-approved, so every tool will prompt -- "
                "including the reads."
            )
    elif agent == "claude-desktop":
        notes += install_json_client(claude_desktop_config(), read_only, dry_run, "claude-desktop")
    elif agent == "cursor":
        notes += install_json_client(cursor_config(), read_only, dry_run, "cursor")
    elif agent == "codex":
        notes += install_codex(read_only, dry_run)
    else:  # pragma: no cover -- argparse and ask_agent both constrain this
        raise InstallError(f"unknown agent {agent!r}")
    return notes


def next_steps(agent: str, read_only: bool) -> list[str]:
    steps = {
        "claude-code": [
            "Restart Claude Code. A server registered mid-session shows as connected",
            "in `claude mcp list` while its tools are still missing from the session",
            "you are in -- the tool list is read once, at startup.",
            "Then check:  claude mcp list | grep apple-ads",
        ],
        "claude-desktop": ["Quit Claude Desktop completely and reopen it."],
        "cursor": ["Restart Cursor, then look for apple-ads under Settings -> MCP."],
        "codex": ["Restart Codex. `codex mcp list` shows what it picked up."],
    }[agent]
    if read_only and agent != "claude-code":
        steps.append("")
        steps.append(
            f"To make this one writable later, remove the {READ_ONLY_ENV} entry from "
            f"its env block and restart the client."
        )
    if SKILL_FILE.exists():
        steps.append("")
        steps.append("The agent will drive this far better with the skill next to it:")
        steps.append(f"  {SKILL_FILE}")
        if agent == "claude-code":
            steps.append("Claude Code finds it automatically when you work in this repo.")
            steps.append("To have it everywhere:")
            steps.append(f"  mkdir -p ~/.claude/skills/{SERVER_NAME}")
            steps.append(f"  cp {SKILL_FILE} ~/.claude/skills/{SERVER_NAME}/")
        else:
            steps.append("Point your agent at that file, or paste it into its rules.")
    return steps


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add the apple-ads MCP server to an AI agent.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--agent",
        choices=sorted(set(AGENTS.values())),
        help="skip the first question",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--read-only",
        dest="read_only",
        action="store_true",
        default=None,
        help="basic permissions only: no tool that can change the account",
    )
    group.add_argument(
        "--no-read-only",
        dest="read_only",
        action="store_false",
        help="install the full tool set",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would change, write nothing"
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="do not ask for confirmation before writing (needs --agent)",
    )
    args = parser.parse_args()

    print("Apple Ads MCP server -- installer")
    print(f"Repository: {REPO_ROOT}\n")

    warnings = preflight()
    for warning in warnings:
        print(f"  !  {warning}\n")

    agent = args.agent or ask_agent()
    read_only = args.read_only if args.read_only is not None else ask_read_only(agent)

    print()
    print(f"Agent:  {agent}")
    print(f"Mode:   {'basic permissions only' if read_only else 'full tool set'}")
    print()
    print("Server block:")
    for line in describe(server_block(read_only and agent != "claude-code")).splitlines():
        print(f"  {line}")
    print()

    if not args.dry_run and not args.yes:
        if input("Write this? [Y/n]: ").strip().lower() in ("n", "no"):
            print("Nothing written.")
            return 0
        print()

    for note in install(agent, read_only, args.dry_run):
        print(f"  {note}")

    print()
    print("Next:")
    for line in next_steps(agent, read_only):
        print(f"  {line}" if line else "")
    if warnings:
        print()
        print("  Note the warning(s) above -- the server is registered but will not")
        print("  work until they are dealt with.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\nCancelled. Nothing written.", file=sys.stderr)
        raise SystemExit(130) from None
