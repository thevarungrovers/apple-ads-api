#!/usr/bin/env python3
"""3f: Apple's recommendations, applied through the ordinary typed kinds.

Run it without touching Apple:
    ./venv/bin/python tests/test_kinds_recommendations.py
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import AD_GROUP, CAMPAIGN, Registrar, failed, rig, run  # noqa: E402

import apple_ads_platform as sdk  # noqa: E402

from apple_ads_mcp import ledger, tools_read  # noqa: E402


def _budget_rec(amount: str):
    return lambda **_: SimpleNamespace(error=None, result=[sdk.DailyCapRecommendation.model_validate({
        "id": "rec-1", "campaignId": CAMPAIGN,
        "suggestedDailyBudgetAmount": {"amount": amount, "currency": "USD"},
        "dailyBudget": {"amount": "100.00", "currency": "USD"},
    })])


def test_a_budget_recommendation_is_a_normal_budget_change() -> None:
    tools, apple, _ = rig()
    apple.extra["query_daily_budget_recommendations"] = _budget_rec("110.00")
    preview = tools["preview_budget_recommendation"](campaign_id=CAMPAIGN)
    assert (preview.before, preview.after) == ("100.00 USD", "110.00 USD")
    assert "rec-1" in preview.note
    result = tools["apply_budget_recommendation"](preview_token=preview.preview_token)
    assert result.applied and apple.campaigns[CAMPAIGN].daily_budget.value.amount == "110.00"
    assert ledger.find_entry(result.entry_id)["field"] == "daily_budget"
    assert "apply_daily_budget_recommendations" not in [m for m, _ in apple.calls]
    undo = tools["revert_change"](entry_id=result.entry_id)
    assert undo.applied and apple.campaigns[CAMPAIGN].daily_budget.value.amount == "100.00"


def test_an_out_of_bounds_recommendation_is_blocked() -> None:
    tools, apple, _ = rig()
    apple.extra["query_daily_budget_recommendations"] = _budget_rec("140.00")
    preview = tools["preview_budget_recommendation"](campaign_id=CAMPAIGN)
    assert preview.blocked and failed(preview, "max_budget_change_pct"), preview.guardrail_checks


def test_a_target_cpa_recommendation_is_a_normal_cpa_goal_change() -> None:
    tools, apple, _ = rig()
    apple.extra["query_target_cpa_recommendations"] = lambda **_: SimpleNamespace(error=None, result=[
        sdk.TargetCpaRecommendation.model_validate({
            "id": "rec-2", "campaignId": CAMPAIGN,
            "recommendedTargetCPA": {"amount": "3.00", "currency": "USD"},
        })])
    preview = tools["preview_target_cpa_recommendation"](ad_group_id=AD_GROUP)
    assert preview.after == "3.00 USD" and not preview.blocked, preview.guardrail_checks
    tools["apply_target_cpa_recommendation"](preview_token=preview.preview_token)
    assert apple.ad_groups[AD_GROUP].cpa_cap.value.amount == "3.00"


def test_no_recommendation_is_reported() -> None:
    tools, apple, _ = rig()
    apple.extra["query_daily_budget_recommendations"] = lambda **_: SimpleNamespace(error=None, result=[])
    try:
        tools["preview_budget_recommendation"](campaign_id=CAMPAIGN)
    except Exception as exc:
        assert "NOT_FOUND" in str(exc)
    else:
        raise AssertionError("expected NOT_FOUND")


def test_the_budget_recommendations_read_shows_both_amounts() -> None:
    _tools, apple, state = rig()
    apple.extra["query_daily_budget_recommendations"] = _budget_rec("110.00")
    registrar = Registrar()
    tools_read.register(registrar, state)
    listed = registrar.tools["budget_recommendations"]()
    row = listed.recommendations[0]
    assert (row.current_daily_budget, row.recommended_daily_budget) == ("100.00 USD", "110.00 USD"), row


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
