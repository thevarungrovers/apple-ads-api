#!/usr/bin/env python3
"""The registered tool set is the whole security boundary. Pin it.

There is no `request(method, path, body)` passthrough in this server, so an
operation that has no tool is unreachable rather than merely undocumented. That
property is only worth anything if the tool set cannot quietly grow, which is
what this file is for: adding a tool without adding it here fails the test, and
the failure names what was added.

The second test is the complement. A handful of Platform API methods are in
scope for the credential and deliberately NOT exposed -- deletes, ad and creative
creation, and the one-call "apply Apple's budget advice". This asserts their
names appear nowhere in tools_write.py, so the refusal is a fact about the code
rather than a decision somebody has to remember.

Run it without touching Apple:
    ./venv/bin/python tests/test_tool_surface.py
    ./venv/bin/python -m pytest tests/ -q      # if pytest is installed
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

import anyio

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apple_ads_mcp.server import mcp  # noqa: E402

READ_TOOLS = frozenset(
    {
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
    }
)

PREVIEW_TOOLS = frozenset(
    {
        "preview_keyword_bid",
        "preview_keyword_bids_bulk",
        "preview_keyword_status",
        "preview_ad_group_status",
        "preview_ad_group_default_bid",
        "preview_campaign_status",
        "preview_campaign_daily_budget",
        "preview_negative_keywords_add",
        "preview_negative_keyword_pause",
    }
)

APPLY_TOOLS = frozenset(
    {
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
    }
)

EXPECTED_TOOLS = READ_TOOLS | PREVIEW_TOOLS | APPLY_TOOLS

# Destructive only where the action moves money in a way that is not a one-call
# undo. Pausing a keyword is reversible; marking it destructive would train you
# to click through the loud prompts, which is how the loud prompts stop working.
DESTRUCTIVE_TOOLS = frozenset({"apply_campaign_status", "apply_campaign_daily_budget"})

# The only two tools that answer from this process without calling Apple, and so
# the only two that carry open_world_hint=False.
LOCAL_ONLY_TOOLS = frozenset({"get_guardrails", "list_my_changes"})

# In scope for the credential, deliberately unreachable from here.
FORBIDDEN_METHODS = (
    "campaigns_post",
    "campaigns_id_delete",
    "adgroups_post",
    "adgroups_id_delete",
    "ads_post",
    "ads_id_put",
    "ads_id_delete",
    "creatives_post",
    "creatives_id_put",
    "creatives_id_delete",
    "keywords_post",
    "keywords_id_delete",
    "negative_keywords_id_delete",
    "negative_keywords_post",
    "shared_budgets_post",
    "shared_budgets_id_put",
    "shared_budgets_id_delete",
    "apply_daily_budget_recommendations",
    "apply_target_cpa_recommendations",
    "upload_asset",
    "delete_asset",
    "ad_accounts_id_put",
    "ad_accounts_post",
)


def _live_strings(source: str) -> list[str]:
    """Every string literal in the module EXCEPT docstrings.

    The forbidden methods are always reached by NAME -- `call("keywords_id_put")`
    -- so the string literals are precisely what has to be searched, and a scan
    that strips them finds nothing however carefully it is written. Docstrings
    are the one class of string to exclude: several of them name these methods
    on purpose, to record why each is absent.

    ast rather than tokenize: comments never reach the tree at all, a docstring
    is identifiable by position rather than by guesswork, and the quoting used
    (single, double, f-string prefix, concatenation) stops mattering.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            continue
        body = getattr(node, "body", None) or []
        first = body[0] if body else None
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            docstrings.add(id(first.value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _tools() -> dict:
    return {tool.name: tool for tool in anyio.run(mcp.list_tools)}


def test_tool_names_match_expected() -> None:
    names = frozenset(_tools())
    added = names - EXPECTED_TOOLS
    removed = EXPECTED_TOOLS - names
    assert not added, f"tools were added without updating this test: {sorted(added)}"
    assert not removed, f"tools disappeared: {sorted(removed)}"


ALL_HINTS = ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")


def test_every_tool_declares_all_four_hints() -> None:
    """An omitted hint is not "unknown" -- it is the spec's default.

    `destructiveHint` defaults to TRUE when absent, so a tool that forgets it is
    published as possibly destructive and gets rendered with the same warning as
    a budget change. Directories that index MCP servers reject a tool whose four
    hints are not all present and boolean. Spell all four out, every time.
    """
    tools = _tools()
    for name, tool in sorted(tools.items()):
        annotations = tool.annotations
        assert annotations is not None, f"{name} has no annotations at all"
        for hint in ALL_HINTS:
            value = getattr(annotations, hint, None)
            assert isinstance(value, bool), (
                f"{name}.{hint} is {value!r}, not an explicit True/False. "
                f"An absent hint falls back on the spec default, which for "
                f"destructiveHint is True."
            )


def test_hints_match_what_the_handler_actually_does() -> None:
    """The hints are a claim about behaviour. Check the claim against the code."""
    tools = _tools()
    for name in READ_TOOLS | PREVIEW_TOOLS:
        a = tools[name].annotations
        assert a.read_only_hint is True, f"{name} reads only, but is not marked read_only_hint"
        assert a.destructive_hint is False, f"{name} reads only, so it cannot be destructive"
        assert a.idempotent_hint is True, f"{name} reads only, so it is idempotent"
    for name in APPLY_TOOLS:
        a = tools[name].annotations
        assert a.read_only_hint is False, f"{name} writes, so it is not read-only"
        assert a.idempotent_hint is False, (
            f"{name} consumes a single-use preview_token, so a second identical "
            f"call is an error rather than a no-op"
        )
    # Only the two local tools answer without touching Apple.
    for name, tool in sorted(tools.items()):
        expected_open_world = name not in LOCAL_ONLY_TOOLS
        assert tool.annotations.open_world_hint is expected_open_world, (
            f"{name} open_world_hint={tool.annotations.open_world_hint}; expected "
            f"{expected_open_world}. Only {sorted(LOCAL_ONLY_TOOLS)} answer without "
            f"calling Apple."
        )


def test_every_read_tool_is_marked_read_only() -> None:
    tools = _tools()
    for name in READ_TOOLS | PREVIEW_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None, f"{name} has no annotations"
        assert annotations.read_only_hint is True, f"{name} is not marked read_only_hint"


def test_no_apply_tool_claims_to_be_read_only() -> None:
    tools = _tools()
    for name in APPLY_TOOLS:
        annotations = tools[name].annotations
        assert annotations is not None, f"{name} has no annotations"
        assert annotations.read_only_hint is False, f"{name} claims read_only_hint"


def test_destructive_hint_is_set_only_where_intended() -> None:
    tools = _tools()
    for name in APPLY_TOOLS:
        hint = bool(tools[name].annotations.destructive_hint)
        assert hint == (name in DESTRUCTIVE_TOOLS), (
            f"{name} destructive_hint={hint}; expected {name in DESTRUCTIVE_TOOLS}. "
            f"Marking a reversible action destructive trains people to click "
            f"through the loud prompts."
        )


def test_every_apply_tool_requires_a_preview_token() -> None:
    tools = _tools()
    for name in APPLY_TOOLS - {"revert_change"}:
        schema = tools[name].input_schema
        assert "preview_token" in (schema.get("required") or []), (
            f"{name} does not require a preview_token, so it could be called cold, "
            f"with no preview sitting above the approval prompt."
        )


def test_out_of_scope_api_methods_are_unreachable() -> None:
    """No forbidden SDK method is named anywhere in the package, in any quoting.

    Scans the WHOLE package, not just tools_write.py: `call()` lives in
    client.py and takes a method NAME, so a forbidden operation reached from
    tools_read.py or from a helper would be just as live, and a scan that looked
    only at tools_write.py would not see it.

    The earlier version matched the literal `"method"` in the raw source, which
    single quotes would slip past. This looks at every non-docstring string in
    the module instead, so the quoting cannot matter.

    The match is on the WHOLE string, not a substring. `call()` takes the method
    name as a complete string, while a tool's `note` field legitimately mentions
    `shared_budgets_id_put` in a sentence explaining why it is absent -- and a
    substring match would fail on that prose forever. A name assembled at
    runtime would slip past this; the registered-tool frozenset above is the
    boundary that does not depend on how a string was spelled.
    """
    forbidden = frozenset(FORBIDDEN_METHODS)
    offenders = []
    for path in sorted((REPO_ROOT / "apple_ads_mcp").glob("*.py")):
        for text in _live_strings(path.read_text()):
            if text.strip() in forbidden:
                offenders.append(f"{path.name}: {text.strip()}")
    assert not offenders, (
        f"out-of-scope API methods are named in live code: {offenders}. If that is "
        f"intentional, each needs a preview/apply pair, a ledger entry and a line "
        f"removed from FORBIDDEN_METHODS."
    )


def test_there_is_no_generic_passthrough() -> None:
    names = frozenset(_tools())
    for suspicious in ("request", "raw_request", "call_api", "http", "execute"):
        assert suspicious not in names, (
            f"a generic {suspicious!r} tool would make every unregistered endpoint "
            f"reachable and defeat the whole surface restriction."
        )


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
    tools = _tools()
    print(
        f"\n{len(tools)} tools registered: {len(READ_TOOLS)} read, "
        f"{len(PREVIEW_TOOLS)} preview, {len(APPLY_TOOLS)} apply."
    )
    print("All checks passed." if not failures else f"{failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
