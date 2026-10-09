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

import pathlib
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


def _tools() -> dict:
    return {tool.name: tool for tool in anyio.run(mcp.list_tools)}


def test_tool_names_match_expected() -> None:
    names = frozenset(_tools())
    added = names - EXPECTED_TOOLS
    removed = EXPECTED_TOOLS - names
    assert not added, f"tools were added without updating this test: {sorted(added)}"
    assert not removed, f"tools disappeared: {sorted(removed)}"


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
    source = (REPO_ROOT / "apple_ads_mcp" / "tools_write.py").read_text()
    # Strip the module docstring: it NAMES the forbidden methods on purpose, to
    # say why each one is absent.
    body = source.split('"""', 2)[-1]
    for method in FORBIDDEN_METHODS:
        assert f'"{method}"' not in body, (
            f"{method} is called from tools_write.py but is out of scope. If that "
            f"is intentional, it needs a preview/apply pair, a ledger entry and a "
            f"line in this list."
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
