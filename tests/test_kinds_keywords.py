#!/usr/bin/env python3
"""3a: adding keywords (PAUSED), changing a match type, negatives in bulk.

Run it without touching Apple:
    ./venv/bin/python tests/test_kinds_keywords.py
"""

from __future__ import annotations

import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import AD_GROUP, KEYWORD, NEGATIVE, failed, rig, run  # noqa: E402

import apple_ads_platform as sdk  # noqa: E402

from apple_ads_mcp import ledger  # noqa: E402


def _raises(fn, needle: str) -> None:
    try:
        fn()
    except Exception as exc:
        assert needle in str(exc), f"expected {needle!r} in {exc!r}"
        return
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def test_add_keywords_creates_them_paused_and_records_their_ids() -> None:
    tools, apple, _ = rig()
    preview = tools["preview_add_keywords"](
        ad_group_id=AD_GROUP, texts=["alpha term", "beta term", "alpha term"], match_type="BROAD",
        bid="0.80",
    )
    assert preview.count == 2, "duplicates in the request are collapsed"
    assert preview.total_projected_daily_spend_delta == "0"
    assert all("PAUSED" in c.after for c in preview.changes)
    result = tools["apply_add_keywords"](preview_token=preview.preview_token)
    assert result.succeeded == 2, result
    created = [int(ledger.find_entry(e)["entity_id"]) for e in result.entry_ids]
    for keyword_id in created:
        keyword = apple.keywords[keyword_id]
        assert keyword.status.value == "PAUSED" and keyword.match_type.value == "BROAD"
        assert keyword.bid.amount == "0.80"


def test_add_keywords_is_blocked_over_max_bid() -> None:
    tools, _apple, _ = rig()
    preview = tools["preview_add_keywords"](ad_group_id=AD_GROUP, texts=["x"], bid="9.00")
    assert preview.blocked and failed(preview, "max_bid"), preview.guardrail_checks


def test_add_keywords_flags_a_duplicate() -> None:
    tools, _apple, _ = rig()
    preview = tools["preview_add_keywords"](ad_group_id=AD_GROUP, texts=["example term"])
    assert any("already exist" in w for w in preview.warnings), preview.warnings


def test_undoing_a_created_keyword_pauses_it_and_never_deletes() -> None:
    tools, apple, _ = rig(max_projected_session_delta=Decimal("500"),
                          max_projected_delta_per_day_total=Decimal("500"))
    preview = tools["preview_add_keywords"](ad_group_id=AD_GROUP, texts=["gamma term"])
    result = tools["apply_add_keywords"](preview_token=preview.preview_token)
    entry_id = result.entry_ids[0]
    new_id = int(ledger.find_entry(entry_id)["entity_id"])
    _raises(lambda: tools["revert_change"](entry_id=entry_id), "already PAUSED")
    enable = tools["preview_keyword_status"](keyword_id=new_id, status="ENABLED")
    tools["apply_keyword_status"](preview_token=enable.preview_token)
    undo = tools["revert_change"](entry_id=entry_id)
    assert undo.applied and apple.keywords[new_id].status.value == "PAUSED"
    assert not [m for m in apple.writes() if "delete" in m]


def test_a_match_type_change_adds_a_paused_copy_and_pauses_the_old() -> None:
    tools, apple, _ = rig(max_projected_session_delta=Decimal("500"),
                          max_projected_delta_per_day_total=Decimal("500"))
    preview = tools["preview_keyword_match_type"](keyword_id=KEYWORD, new_match_type="BROAD")
    assert preview.count == 2 and not preview.blocked, preview.guardrail_checks
    assert Decimal(preview.total_projected_daily_spend_delta) < 0
    result = tools["apply_keyword_match_type"](preview_token=preview.preview_token)
    assert result.succeeded == 2, result
    new_id = int(ledger.find_entry(result.entry_ids[0])["entity_id"])
    assert apple.keywords[new_id].match_type.value == "BROAD"
    assert apple.keywords[new_id].status.value == "PAUSED"
    assert apple.keywords[KEYWORD].status.value == "PAUSED"
    undo = tools["revert_change"](entry_id=result.entry_id)
    assert undo.applied, undo.failures
    assert apple.keywords[KEYWORD].status.value == "ENABLED"
    # Re-enabling the old keyword restores spend, and its row records it, so the
    # daily cap (which sums rows) sees it.
    rows = [r for r in ledger.entries() if r["tool"] == "revert_change"]
    assert [r["projected_daily_spend_delta"] for r in rows] == ["90.00"], rows
    assert apple.keywords[new_id].status.value == "PAUSED"


def test_a_match_type_pause_that_times_out_is_unknown_not_failed() -> None:
    """The copy was created, then the pause of the old keyword timed out: it may
    have landed, so its row is UNKNOWN and counted -- never FAILED."""
    from mcp.server.mcpserver.exceptions import ToolError

    from apple_ads_mcp.guardrails import DailyUsage

    tools, apple, state = rig(max_projected_session_delta=Decimal("500"),
                              max_projected_delta_per_day_total=Decimal("500"))
    preview = tools["preview_keyword_match_type"](keyword_id=KEYWORD, new_match_type="BROAD")
    apple.fail["keywords_id_put"] = ToolError(
        "TRANSIENT: keywords_id_put failed to reach Apple: TimeoutError: read timed out")
    result = tools["apply_keyword_match_type"](preview_token=preview.preview_token)
    create_row, pause_row = (ledger.find_entry(e) for e in result.entry_ids)
    assert create_row["outcome"] == ledger.APPLIED, create_row
    assert pause_row["outcome"] == ledger.UNKNOWN, pause_row
    assert any("UNKNOWN" in f.upper() for f in result.failures), result.failures
    assert state.counters.snapshot()["applies_this_session"] == 1
    assert DailyUsage(state.limits).usage()[0] == 1


def test_a_match_type_change_to_the_same_type_is_refused() -> None:
    tools, _apple, _ = rig()
    _raises(lambda: tools["preview_keyword_match_type"](keyword_id=KEYWORD, new_match_type="EXACT"),
            "already EXACT")


def test_negative_status_in_bulk_projects_each_campaign_once() -> None:
    tools, apple, _ = rig(max_projected_delta_per_day_total=Decimal("500"),
                          max_projected_session_delta=Decimal("500"))
    apple.negatives[NEGATIVE + 1] = apple.negatives[NEGATIVE].model_copy(
        update={"id": NEGATIVE + 1, "text": "gratis"}
    )
    preview = tools["preview_negative_keyword_status"](
        negative_keyword_ids=[NEGATIVE, NEGATIVE + 1], status="PAUSED"
    )
    assert preview.total_projected_daily_spend_delta == "90.00", preview.total_projected_daily_spend_delta
    assert any(c.startswith("SKIP projected_daily_delta") for c in preview.guardrail_checks)
    result = tools["apply_negative_keyword_status"](preview_token=preview.preview_token)
    assert result.succeeded == 2
    assert apple.negatives[NEGATIVE + 1].status.value == "PAUSED"
    undo = tools["revert_change"](entry_id=result.entry_id)
    assert undo.applied and apple.negatives[NEGATIVE].status.value == "ENABLED"
    # Re-enabling a negative narrows: the revert restores no spend, row by row.
    assert undo.projected_daily_spend_delta == "0"


def _two_negatives(apple) -> None:
    apple.negatives[NEGATIVE + 1] = apple.negatives[NEGATIVE].model_copy(
        update={"id": NEGATIVE + 1, "text": "gratis"}
    )


def test_resolving_an_unknown_bulk_negative_pause_keeps_the_campaign_bound() -> None:
    """The batch timed out; only the SECOND item landed. Settling the first as
    failed must move the campaign's bound onto the second, not lose it."""
    from mcp.server.mcpserver.exceptions import ToolError

    from apple_ads_mcp.guardrails import DailyUsage

    tools, apple, state = rig(max_projected_delta_per_day_total=Decimal("500"),
                              max_projected_session_delta=Decimal("500"))
    _two_negatives(apple)
    preview = tools["preview_negative_keyword_status"](
        negative_keyword_ids=[NEGATIVE, NEGATIVE + 1], status="PAUSED"
    )

    def second_lands_then_times_out(**_):
        apple.negatives[NEGATIVE + 1] = apple.negatives[NEGATIVE + 1].model_copy(
            update={"status": sdk.NegativeKeywordStatus("PAUSED")})
        raise ToolError("TRANSIENT: negative_keywords_bulk_update_post: read timed out")

    apple._negative_keywords_bulk_update_post = second_lands_then_times_out
    try:
        tools["apply_negative_keyword_status"](preview_token=preview.preview_token)
    except ToolError:
        pass
    [batch_id] = {r["batch_id"] for r in ledger.entries()}
    first, second = ledger.batch_entries(batch_id)
    assert (first["outcome"], second["outcome"]) == (ledger.UNKNOWN, ledger.UNKNOWN)
    assert DailyUsage(state.limits).usage()[1] == Decimal("90.00")

    settled = tools["resolve_unknown_change"](entry_id=first["entry_id"])
    assert settled.settled == {first["entry_id"]: ledger.FAILED}, settled
    # The traffic may be back through the second item: the bound must survive.
    assert DailyUsage(state.limits).usage()[1] == Decimal("90.00")
    tools["resolve_unknown_change"](entry_id=second["entry_id"])
    assert ledger.find_entry(second["entry_id"])["outcome"] == ledger.APPLIED
    assert Decimal(ledger.find_entry(second["entry_id"])["projected_daily_spend_delta"]) == Decimal("90.00")
    assert DailyUsage(state.limits).usage()[1] == Decimal("90.00")


def test_a_partial_negative_pause_still_counts_its_campaign_bound() -> None:
    from apple_ads_mcp import ledger
    from apple_ads_mcp.guardrails import DailyUsage

    tools, apple, state = rig(max_projected_delta_per_day_total=Decimal("500"),
                              max_projected_session_delta=Decimal("500"))
    apple.negatives[NEGATIVE + 1] = apple.negatives[NEGATIVE].model_copy(
        update={"id": NEGATIVE + 1, "text": "gratis"}
    )
    preview = tools["preview_negative_keyword_status"](
        negative_keyword_ids=[NEGATIVE, NEGATIVE + 1], status="PAUSED"
    )
    real = apple._negative_keywords_bulk_update_post

    def first_fails(negative_keyword_update_bulk_request, **kw):
        response = real(negative_keyword_update_bulk_request=negative_keyword_update_bulk_request, **kw)
        response.result[0].success, response.result[0].error = False, "INVALID"
        return response

    apple._negative_keywords_bulk_update_post = first_fails
    result = tools["apply_negative_keyword_status"](preview_token=preview.preview_token)
    assert (result.succeeded, result.failed) == (1, 1), result
    # The second item landed, so its campaign's traffic is back: bounded once.
    assert result.projected_daily_spend_delta == "90.00", result.projected_daily_spend_delta
    assert state.counters.snapshot()["projected_session_delta"] == "90.00"
    assert DailyUsage(state.limits).usage()[1] == Decimal("90.00")
    landed = ledger.find_entry(result.entry_ids[1])
    assert Decimal(landed["projected_daily_spend_delta"]) == Decimal("90.00"), landed


def test_a_stale_negative_status_is_refused() -> None:
    tools, apple, _ = rig()
    preview = tools["preview_negative_keyword_status"](negative_keyword_ids=[NEGATIVE], status="PAUSED")
    apple.negatives[NEGATIVE] = apple.negatives[NEGATIVE].model_copy(
        update={"status": sdk.NegativeKeywordStatus("PAUSED")}
    )
    _raises(lambda: tools["apply_negative_keyword_status"](preview_token=preview.preview_token),
            "Something changed it in between")


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
