#!/usr/bin/env python3
"""The change engine, through the eight pairs ported onto it.

Every test drives the real tool functions against a FakeApple (tests/kindkit.py)
and asserts on what was sent, what the ledger recorded, and what revert does.

Run it without touching Apple:
    ./venv/bin/python tests/test_changes_framework.py
"""

from __future__ import annotations

import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import (  # noqa: E402
    AD_GROUP,
    CAMPAIGN,
    KEYWORD,
    NEGATIVE,
    failed,
    rig,
    run,
)

from apple_ads_mcp import ledger  # noqa: E402


def _raises(fn, needle: str) -> str:
    try:
        fn()
    except Exception as exc:  # ToolError
        assert needle in str(exc), f"expected {needle!r} in {exc!r}"
        return str(exc)
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def test_keyword_bid_previews_applies_and_reverts() -> None:
    tools, apple, _ = rig()
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.20")
    assert preview.before == "1.00 USD" and preview.after == "1.20 USD", preview
    assert not preview.blocked, preview.guardrail_checks
    # 35 taps over 7 days at +0.20: 5/day * 0.20.
    assert preview.projected_daily_spend_delta == "1.00", preview.projected_daily_spend_delta

    result = tools["apply_keyword_bid"](preview_token=preview.preview_token)
    assert result.applied, result.failures
    assert apple.keywords[KEYWORD].bid.amount == "1.20"
    row = ledger.find_entry(result.entry_id)
    assert row["outcome"] == "applied" and row["before"] == "1.00" and row["after"] == "1.20"

    undo = tools["revert_change"](entry_id=result.entry_id)
    assert undo.applied, undo.failures
    assert apple.keywords[KEYWORD].bid.amount == "1.00"
    assert result.entry_id in ledger.reverted_entry_ids()


def test_a_token_is_single_use_and_bound_to_its_tool() -> None:
    tools, _apple, _ = rig()
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.10")
    _raises(lambda: tools["apply_keyword_status"](preview_token=preview.preview_token),
            "minted for apply_keyword_bid")
    # A mismatched tool does not consume it.
    tools["apply_keyword_bid"](preview_token=preview.preview_token)
    _raises(lambda: tools["apply_keyword_bid"](preview_token=preview.preview_token),
            "unknown or already used")


def test_a_guardrail_fail_blocks_the_apply() -> None:
    tools, apple, _ = rig()
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="9.00")
    assert preview.blocked and failed(preview, "max_bid"), preview.guardrail_checks
    _raises(lambda: tools["apply_keyword_bid"](preview_token=preview.preview_token),
            "blocked by a guardrail")
    assert apple.writes() == []


def test_a_stale_value_is_refused_and_nothing_is_sent() -> None:
    tools, apple, _ = rig()
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.10")
    apple.keywords[KEYWORD] = apple.keywords[KEYWORD].model_copy(
        update={"bid": apple.keywords[KEYWORD].bid.model_copy(update={"amount": "1.05"})}
    )
    _raises(lambda: tools["apply_keyword_bid"](preview_token=preview.preview_token),
            "Something changed it in between")
    assert apple.writes() == []


def test_a_discarded_write_is_recorded_as_failed() -> None:
    tools, apple, _ = rig()
    preview = tools["preview_ad_group_default_bid"](ad_group_id=AD_GROUP, new_bid="1.10")
    apple.ignore_writes = True
    result = tools["apply_ad_group_default_bid"](preview_token=preview.preview_token)
    assert result.applied is False and "NOT APPLIED" in result.note
    assert ledger.find_entry(result.entry_id)["outcome"] == "failed"
    _raises(lambda: tools["revert_change"](entry_id=result.entry_id), "not 'applied'")


def test_the_default_bid_write_echoes_the_bid_strategy() -> None:
    tools, apple, _ = rig()
    preview = tools["preview_ad_group_default_bid"](ad_group_id=AD_GROUP, new_bid="1.10")
    tools["apply_ad_group_default_bid"](preview_token=preview.preview_token)
    method, kwargs = [c for c in apple.calls if c[0] == "adgroups_id_put"][0]
    strategy = kwargs["ad_group_update"].bid_strategy
    assert strategy.bid_strategy_type.value == "MANUAL_CPT"
    assert strategy.bid_strategy_goal.value == "TAP"


def test_the_kill_switch_stops_an_apply() -> None:
    from apple_ads_mcp import tools_write

    tools, apple, _ = rig()
    preview = tools["preview_campaign_daily_budget"](campaign_id=CAMPAIGN, new_daily_budget="110.00")
    tools_write.writes_disabled = lambda: "testing"
    _raises(lambda: tools["apply_campaign_daily_budget"](preview_token=preview.preview_token),
            "writes are disabled")
    assert apple.writes() == []


def test_a_bulk_bid_change_writes_one_ledger_row_per_keyword() -> None:
    tools, apple, _ = rig()
    second = apple.keywords[KEYWORD].model_copy(update={"id": KEYWORD + 1, "text": "second term"})
    apple.keywords[KEYWORD + 1] = second
    preview = tools["preview_keyword_bids_bulk"](
        keyword_ids=[KEYWORD, KEYWORD + 1], new_bids=["1.10", "0.90"]
    )
    assert preview.count == 2 and not preview.blocked, preview.guardrail_checks
    result = tools["apply_keyword_bids_bulk"](preview_token=preview.preview_token)
    assert result.succeeded == 2, result
    assert len(result.entry_ids) == 2
    rows = [ledger.find_entry(e) for e in result.entry_ids]
    assert {r["entity_id"] for r in rows} == {str(KEYWORD), str(KEYWORD + 1)}
    assert rows[0]["batch_id"] == rows[1]["batch_id"] == result.entry_id

    # One item on its own...
    one = tools["revert_change"](entry_id=result.entry_ids[0])
    assert one.applied and apple.keywords[KEYWORD].bid.amount == "1.00"
    # ...then the rest of the batch by its id.
    rest = tools["revert_change"](entry_id=result.entry_id)
    assert rest.applied and apple.keywords[KEYWORD + 1].bid.amount == "1.00"


def test_negative_adds_record_the_created_ids_and_revert_by_pausing() -> None:
    tools, apple, _ = rig()
    preview = tools["preview_negative_keywords_add"](
        texts=["cheap", "torrent"], match_type="EXACT", ad_group_id=AD_GROUP
    )
    assert not preview.blocked, preview.guardrail_checks
    result = tools["apply_negative_keywords_add"](preview_token=preview.preview_token)
    assert result.succeeded == 2, result
    rows = [ledger.find_entry(e) for e in result.entry_ids]
    created = [int(r["entity_id"]) for r in rows]
    assert all(c in apple.negatives for c in created), created
    assert rows[0]["field"] == "created" and rows[0]["after"] == "ENABLED"

    undo = tools["revert_change"](entry_id=result.entry_ids[0])
    assert undo.applied, undo.failures
    assert apple.negatives[created[0]].status.value == "PAUSED"
    assert "negative_keywords_id_delete" not in apple.writes()


def test_status_pairs_round_trip() -> None:
    # Each revert re-enables, which counts its whole bound against the session
    # and the day -- so give this round trip room for four of them.
    big = Decimal("1000")
    tools, apple, _ = rig(max_projected_session_delta=big, max_projected_delta_per_day_total=big)
    cases = [
        ("keyword_status", {"keyword_id": KEYWORD}, lambda: apple.keywords[KEYWORD].status),
        ("ad_group_status", {"ad_group_id": AD_GROUP}, lambda: apple.ad_groups[AD_GROUP].status),
        ("campaign_status", {"campaign_id": CAMPAIGN}, lambda: apple.campaigns[CAMPAIGN].status),
        ("negative_keyword_pause", {"negative_keyword_id": NEGATIVE},
         lambda: apple.negatives[NEGATIVE].status),
    ]
    for name, args, read in cases:
        preview = tools[f"preview_{name}"](status="PAUSED", **args)
        result = tools[f"apply_{name}"](preview_token=preview.preview_token)
        assert result.applied and read().value == "PAUSED", (name, result)
        undo = tools["revert_change"](entry_id=result.entry_id)
        assert undo.applied and read().value == "ENABLED", (name, undo)


def test_legacy_rows_are_refused_with_their_reason() -> None:
    tools, _apple, _ = rig()
    for entry_id, tool, entity_id, field in (
        ("legacy-bulk", "apply_keyword_bids_bulk", "1,2", "bid"),
        ("legacy-neg", "apply_negative_keywords_add", "(new)", "exists"),
    ):
        ledger.write_intent(
            entry_id, tool=tool, entity_type="keyword", entity_id=entity_id,
            entity_name="x", path="p", field=field, before="1.00", after="2.00",
            campaign_id=CAMPAIGN, projected_daily_spend_delta="0",
        )
        ledger.write_outcome(entry_id, outcome="applied")
    _raises(lambda: tools["revert_change"](entry_id="legacy-bulk"), "bulk bid change")
    _raises(lambda: tools["revert_change"](entry_id="legacy-neg"), "Pause them")


def test_one_report_per_campaign_in_a_bulk_preview() -> None:
    tools, apple, _ = rig()
    for offset in range(1, 4):
        apple.keywords[KEYWORD + offset] = apple.keywords[KEYWORD].model_copy(
            update={"id": KEYWORD + offset}
        )
    tools["preview_keyword_bids_bulk"](
        keyword_ids=[KEYWORD + o for o in range(4)], new_bids=["1.05"] * 4
    )
    reports = [m for m, _ in apple.calls if m.endswith("_without_preload_content")]
    campaigns = [m for m, _ in apple.calls if m == "campaigns_id_get"]
    assert len(reports) == 1, reports
    assert len(campaigns) == 1, campaigns


def test_a_keyword_without_its_own_bid_is_measured_against_the_default() -> None:
    tools, apple, _ = rig()
    apple.keywords[KEYWORD] = apple.keywords[KEYWORD].model_copy(update={"bid": None})
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.20")
    assert preview.before == "1.00 USD (ad group default)", preview.before
    assert preview.after == "1.20 USD"
    assert preview.projected_daily_spend_delta == "1.00", preview.projected_daily_spend_delta
    assert preview.pct_change == "+20.0%"
    assert not failed(preview, "currency"), preview.guardrail_checks
    assert any("no bid of its own" in w for w in preview.warnings)
    over = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="2.00")
    assert failed(over, "max_bid_increase_pct"), over.guardrail_checks
    result = tools["apply_keyword_bid"](preview_token=preview.preview_token)
    assert result.applied
    _raises(lambda: tools["revert_change"](entry_id=result.entry_id), "no previous value")


def test_a_campaign_level_negative_add_says_duplicates_were_not_checked() -> None:
    tools, apple, _ = rig()
    preview = tools["preview_negative_keywords_add"](texts=["free"], campaign_id=CAMPAIGN)
    assert any("duplicates could not be checked" in w for w in preview.warnings), preview.warnings
    assert not [m for m, _ in apple.calls if m == "negative_keywords_query_post"], (
        "a campaignId-only negative query is rejected by Apple; it must not be sent"
    )


def test_an_ad_group_negative_add_flags_an_existing_duplicate() -> None:
    tools, _apple, _ = rig()
    preview = tools["preview_negative_keywords_add"](
        texts=["free", "new"], match_type="BROAD", ad_group_id=AD_GROUP
    )
    assert any("1 of these already exist" in w for w in preview.warnings), preview.warnings


def test_revert_rechecks_the_campaign_scope() -> None:
    import dataclasses

    tools, _apple, state = rig()
    preview = tools["preview_keyword_status"](keyword_id=KEYWORD, status="PAUSED")
    result = tools["apply_keyword_status"](preview_token=preview.preview_token)
    state.limits = dataclasses.replace(state.limits, allowed_campaign_ids=frozenset({999}))
    _raises(lambda: tools["revert_change"](entry_id=result.entry_id), "NOT in the pinned set")


def test_a_partial_bulk_records_only_what_landed() -> None:
    tools, apple, state = rig()
    apple.keywords[KEYWORD + 1] = apple.keywords[KEYWORD].model_copy(update={"id": KEYWORD + 1})
    apple.reports["apps_keyword_reports"].append(apple.report_row(KEYWORD + 1, spend="35.00", taps=35))
    preview = tools["preview_keyword_bids_bulk"](
        keyword_ids=[KEYWORD, KEYWORD + 1], new_bids=["1.20", "1.40"]
    )
    assert preview.total_projected_daily_spend_delta == "3.00", preview.total_projected_daily_spend_delta
    real = apple._keywords_bulk_update_post

    def half(keyword_update_bulk_request, **kw):
        response = real(keyword_update_bulk_request=keyword_update_bulk_request, **kw)
        response.result[1].success, response.result[1].error = False, "INVALID bid"
        return response

    apple._keywords_bulk_update_post = half
    result = tools["apply_keyword_bids_bulk"](preview_token=preview.preview_token)
    assert (result.succeeded, result.failed) == (1, 1), result
    assert result.projected_daily_spend_delta == "1.00", result.projected_daily_spend_delta
    assert state.counters.snapshot()["projected_session_delta"] == "1.00"
    outcomes = [ledger.find_entry(e)["outcome"] for e in result.entry_ids]
    assert outcomes == ["applied", "failed"], outcomes



def _up_and_down(apple) -> None:
    """KEYWORD+1 alongside KEYWORD, both 35 taps/7d at 1.00."""
    apple.keywords[KEYWORD + 1] = apple.keywords[KEYWORD].model_copy(update={"id": KEYWORD + 1})
    apple.reports["apps_keyword_reports"].append(apple.report_row(KEYWORD + 1, spend="35.00", taps=35))


def test_a_bulk_bid_item_over_the_per_change_limit_blocks_even_when_the_net_is_zero() -> None:
    tools, apple, _ = rig(max_projected_delta_per_day=Decimal("2.00"))
    _up_and_down(apple)
    # +0.50 on 5 taps/day is +2.50; -0.50 is -2.50. Net 0, but one item is over 2.00.
    preview = tools["preview_keyword_bids_bulk"](
        keyword_ids=[KEYWORD, KEYWORD + 1], new_bids=["1.50", "0.50"]
    )
    assert preview.blocked, preview.guardrail_checks
    assert failed(preview, "projected_daily_delta"), preview.guardrail_checks


def test_a_bulk_bid_decrease_does_not_offset_an_increase_in_the_caps() -> None:
    from apple_ads_mcp.guardrails import DailyUsage

    tools, apple, state = rig()
    _up_and_down(apple)
    preview = tools["preview_keyword_bids_bulk"](
        keyword_ids=[KEYWORD, KEYWORD + 1], new_bids=["1.50", "0.50"]
    )
    assert preview.total_projected_daily_spend_delta == "2.50", preview.total_projected_daily_spend_delta
    result = tools["apply_keyword_bids_bulk"](preview_token=preview.preview_token)
    assert result.succeeded == 2, result
    assert result.projected_daily_spend_delta == "2.50", result.projected_daily_spend_delta
    assert state.counters.snapshot()["projected_session_delta"] == "2.50"
    assert DailyUsage(state.limits).usage()[1] == Decimal("2.50")


def test_a_bulk_bid_netting_to_zero_is_still_refused_by_the_session_cap() -> None:
    tools, apple, _ = rig(max_projected_session_delta=Decimal("2.00"))
    _up_and_down(apple)
    preview = tools["preview_keyword_bids_bulk"](
        keyword_ids=[KEYWORD, KEYWORD + 1], new_bids=["1.50", "0.50"]
    )
    _raises(
        lambda: tools["apply_keyword_bids_bulk"](preview_token=preview.preview_token),
        "session spend limit",
    )
    assert apple.writes() == []


def test_a_partial_bulk_where_only_the_decrease_landed_records_nothing() -> None:
    from apple_ads_mcp.guardrails import DailyUsage

    tools, apple, state = rig()
    _up_and_down(apple)
    preview = tools["preview_keyword_bids_bulk"](
        keyword_ids=[KEYWORD, KEYWORD + 1], new_bids=["1.50", "0.50"]
    )
    real = apple._keywords_bulk_update_post

    def first_fails(keyword_update_bulk_request, **kw):
        response = real(keyword_update_bulk_request=keyword_update_bulk_request, **kw)
        response.result[0].success, response.result[0].error = False, "INVALID bid"
        return response

    apple._keywords_bulk_update_post = first_fails
    result = tools["apply_keyword_bids_bulk"](preview_token=preview.preview_token)
    assert (result.succeeded, result.failed) == (1, 1), result
    assert result.projected_daily_spend_delta == "0", result.projected_daily_spend_delta
    assert state.counters.snapshot()["projected_session_delta"] == "0"
    assert DailyUsage(state.limits).usage()[1] == Decimal("0")



TIMEOUT = "TRANSIENT: keywords_id_put failed to reach Apple: TimeoutError: read timed out"


def _timeout_after_landing(apple, method: str) -> None:
    """The write reaches Apple and lands; the answer never comes back."""
    from mcp.server.mcpserver.exceptions import ToolError

    real = getattr(apple, f"_{method}")

    def lands_then_times_out(**kwargs):
        real(**kwargs)
        raise ToolError(TIMEOUT)

    setattr(apple, f"_{method}", lands_then_times_out)


def test_a_timed_out_write_is_left_unknown_and_counted_against_the_caps() -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    from apple_ads_mcp.guardrails import DailyUsage

    tools, apple, state = rig()
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.20")
    apple.fail["keywords_id_put"] = ToolError(TIMEOUT)
    message = _raises(
        lambda: tools["apply_keyword_bid"](preview_token=preview.preview_token),
        "reconcile_ledger",
    )
    assert "may have landed" in message, message
    [row] = ledger.entries()
    assert row["outcome"] == "unknown", row["outcome"]
    snapshot = state.counters.snapshot()
    assert snapshot["applies_this_session"] == 1, snapshot
    assert snapshot["projected_session_delta"] == "1.00", snapshot
    assert DailyUsage(state.limits).usage() == (1, Decimal("1.00"))


def test_a_rejected_write_is_still_failed_and_costs_nothing() -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    tools, apple, state = rig()
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.20")
    apple.fail["keywords_id_put"] = ToolError("INVALID: Apple returned HTTP 400 for keywords_id_put")
    _raises(lambda: tools["apply_keyword_bid"](preview_token=preview.preview_token), "INVALID")
    [row] = ledger.entries()
    assert row["outcome"] == "failed", row["outcome"]
    assert state.counters.snapshot()["applies_this_session"] == 0


def test_a_timed_out_creation_warns_that_a_retry_may_duplicate() -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    tools, apple, _ = rig()
    preview = tools["preview_negative_keywords_add"](texts=["one"], ad_group_id=AD_GROUP)
    apple.fail["negative_keywords_bulk_create_post"] = ToolError(TIMEOUT)
    message = _raises(
        lambda: tools["apply_negative_keywords_add"](preview_token=preview.preview_token),
        "duplicate",
    )
    assert "reconcile_ledger" in message, message
    assert [r["outcome"] for r in ledger.entries()] == ["unknown"]


def test_reconcile_lists_writes_whose_outcome_is_unknown() -> None:
    from types import SimpleNamespace

    from mcp.server.mcpserver.exceptions import ToolError

    from apple_ads_mcp import tools_read
    from kindkit import Registrar

    tools, apple, state = rig()
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.20")
    apple.fail["keywords_id_put"] = ToolError(TIMEOUT)
    _raises(lambda: tools["apply_keyword_bid"](preview_token=preview.preview_token), "TRANSIENT")
    apple.extra["query_audit_summary"] = lambda **_: SimpleNamespace(result=[], error=None)
    registrar = Registrar()
    tools_read.register(registrar, state)
    report = registrar.tools["reconcile_ledger"](days=1)
    assert [e.entity_id for e in report.unknown_outcome] == [str(KEYWORD)], report.unknown_outcome
    assert "unknown_outcome" in report.note, report.note


def test_reverting_an_unknown_write_that_landed_confirms_it_then_reverts() -> None:
    tools, apple, _ = rig()
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.20")
    _timeout_after_landing(apple, "keywords_id_put")
    _raises(lambda: tools["apply_keyword_bid"](preview_token=preview.preview_token), "TRANSIENT")
    assert apple.keywords[KEYWORD].bid.amount == "1.20"
    [row] = ledger.entries()
    del apple._keywords_id_put  # the timeout was a one-off
    undo = tools["revert_change"](entry_id=row["entry_id"])
    assert undo.applied, undo.failures
    assert apple.keywords[KEYWORD].bid.amount == "1.00"
    assert ledger.find_entry(row["entry_id"])["outcome"] == "applied"
    assert row["entry_id"] in ledger.reverted_entry_ids()


def test_reverting_an_unknown_write_that_never_landed_says_so() -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    tools, apple, _ = rig()
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.20")
    apple.fail["keywords_id_put"] = ToolError(TIMEOUT)
    _raises(lambda: tools["apply_keyword_bid"](preview_token=preview.preview_token), "TRANSIENT")
    [row] = ledger.entries()
    del apple.fail["keywords_id_put"]
    _raises(lambda: tools["revert_change"](entry_id=row["entry_id"]), "never landed")
    assert apple.writes() == ["keywords_id_put"]  # only the original attempt
    assert ledger.find_entry(row["entry_id"])["outcome"] == "failed"



def _three_bid_batch(tools, apple):
    for n in (1, 2):
        apple.keywords[KEYWORD + n] = apple.keywords[KEYWORD].model_copy(
            update={"id": KEYWORD + n, "text": f"term {n}"})
    preview = tools["preview_keyword_bids_bulk"](
        keyword_ids=[KEYWORD, KEYWORD + 1, KEYWORD + 2], new_bids=["1.10", "1.10", "1.10"])
    result = tools["apply_keyword_bids_bulk"](preview_token=preview.preview_token)
    assert result.succeeded == 3, result
    return result


def _nth_revert_put_fails(apple, n: int, exc: Exception) -> None:
    real = apple._keywords_id_put
    seen = {"calls": 0}

    def put(**kwargs):
        seen["calls"] += 1
        if seen["calls"] == n:
            raise exc
        return real(**kwargs)

    apple._keywords_id_put = put


def test_a_refused_item_mid_batch_revert_leaves_no_row_unfinished() -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    tools, apple, state = rig()
    result = _three_bid_batch(tools, apple)
    before = state.counters.snapshot()["applies_this_session"]
    _nth_revert_put_fails(apple, 2, ToolError("INVALID: Apple returned HTTP 400 for keywords_id_put"))
    _raises(lambda: tools["revert_change"](entry_id=result.entry_id), "INVALID")
    by_original = {r["reverts_entry_id"]: r for r in ledger.entries() if r["tool"] == "revert_change"}
    first, second, third = (by_original[e] for e in result.entry_ids)
    assert first["outcome"] == ledger.APPLIED, first
    assert second["outcome"] == ledger.FAILED, second
    assert third["outcome"] == ledger.FAILED, third
    assert "not sent: batch stopped at item 2" in third["outcome_detail"], third
    # The item that landed is charged to the session.
    assert state.counters.snapshot()["applies_this_session"] == before + 1


def test_an_unknown_item_mid_batch_revert_is_charged_once() -> None:
    from mcp.server.mcpserver.exceptions import ToolError

    tools, apple, state = rig()
    result = _three_bid_batch(tools, apple)
    before = state.counters.snapshot()["applies_this_session"]
    _nth_revert_put_fails(apple, 2, ToolError(TIMEOUT))
    _raises(lambda: tools["revert_change"](entry_id=result.entry_id), "TRANSIENT")
    by_original = {r["reverts_entry_id"]: r for r in ledger.entries() if r["tool"] == "revert_change"}
    first, second, third = (by_original[e] for e in result.entry_ids)
    assert (first["outcome"], second["outcome"], third["outcome"]) == (
        ledger.APPLIED, ledger.UNKNOWN, ledger.FAILED), (first, second, third)
    assert "not sent: batch stopped at item 2" in third["outcome_detail"], third
    assert state.counters.snapshot()["applies_this_session"] == before + 1


def test_an_inheriting_keyword_bid_refuses_a_moved_ad_group_default() -> None:
    tools, apple, _ = rig()
    apple.keywords[KEYWORD] = apple.keywords[KEYWORD].model_copy(update={"bid": None})
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.20")
    assert "ad group default" in preview.before, preview.before
    # The default the projection and the % check were measured against moves.
    group = apple.ad_groups[AD_GROUP]
    apple.ad_groups[AD_GROUP] = group.model_copy(update={"bid_strategy": group.bid_strategy.model_copy(
        update={"bid": group.bid_strategy.bid.model_copy(update={"amount": "0.50"})})})
    _raises(
        lambda: tools["apply_keyword_bid"](preview_token=preview.preview_token),
        "Something changed it in between",
    )
    assert apple.writes() == []



def test_money_rejects_exponents_and_other_non_plain_spellings() -> None:
    from apple_ads_mcp.client import parse_decimal

    for raw in ("5E+1", "5e1", "1E-2", "1.2e0", "1_000", "+1.20", ".5", "1.", "1,20",
                "\uff11.\uff12\uff10", "0x10", "NaN", "Infinity", "-1.00"):
        _raises(lambda raw=raw: parse_decimal(raw, "new_bid"), "new_bid")
    assert str(parse_decimal("1.20", "x")) == "1.20"
    assert str(parse_decimal(" 0.5 ", "x")) == "0.5"
    assert str(parse_decimal("007", "x")) == "7"
    assert str(parse_decimal("10", "x")) == "10"


def test_an_exponent_bid_is_refused_before_anything_is_previewed() -> None:
    tools, apple, _ = rig()
    _raises(lambda: tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="5E-1"), "plain decimal")
    assert apple.writes() == []


def test_money_is_always_sent_as_a_plain_string() -> None:
    from apple_ads_mcp.client import to_money

    assert to_money(Decimal("5E+1"), "USD").amount == "50"
    assert to_money("1.20", "USD").amount == "1.20"


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
