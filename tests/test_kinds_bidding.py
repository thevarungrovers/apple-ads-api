#!/usr/bin/env python3
"""3b: bid strategies, CPA goals and campaign schedules.

Run it without touching Apple:
    ./venv/bin/python tests/test_kinds_bidding.py
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import AD_GROUP, CAMPAIGN, failed, rig, run  # noqa: E402

import apple_ads_platform as sdk  # noqa: E402

ROOMY = dict(max_projected_session_delta=Decimal("500"), max_projected_delta_per_day_total=Decimal("500"))


def _raises(fn, needle: str) -> None:
    try:
        fn()
    except Exception as exc:
        assert needle in str(exc), f"expected {needle!r} in {exc!r}"
        return
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def test_handing_bids_to_apple_is_bounded_and_round_trips() -> None:
    tools, apple, _ = rig(**ROOMY)
    preview = tools["preview_ad_group_bid_strategy"](
        ad_group_id=AD_GROUP, bid_strategy_type="MAX_CONVERSIONS", bid_strategy_goal="INSTALL"
    )
    assert (preview.before, preview.after) == ("MANUAL_CPT/TAP", "MAX_CONVERSIONS/INSTALL")
    assert preview.projected_daily_spend_delta == "90.00"
    assert any("stop being used" in w for w in preview.warnings), preview.warnings
    result = tools["apply_ad_group_bid_strategy"](preview_token=preview.preview_token)
    assert result.applied, result.failures
    assert apple.ad_groups[AD_GROUP].bid_strategy.bid_strategy_type.value == "MAX_CONVERSIONS"
    undo = tools["revert_change"](entry_id=result.entry_id)
    assert undo.applied and apple.ad_groups[AD_GROUP].bid_strategy.bid_strategy_type.value == "MANUAL_CPT"


def test_a_stale_strategy_is_refused() -> None:
    tools, apple, _ = rig(**ROOMY)
    preview = tools["preview_ad_group_bid_strategy"](ad_group_id=AD_GROUP, bid_strategy_type="MAX_CONVERSIONS")
    group = apple.ad_groups[AD_GROUP]
    apple.ad_groups[AD_GROUP] = group.model_copy(update={"bid_strategy": group.bid_strategy.model_copy(
        update={"bid_strategy_goal": sdk.BidStrategyGoal("INSTALL")})})
    _raises(lambda: tools["apply_ad_group_bid_strategy"](preview_token=preview.preview_token),
            "Something changed it in between")


def _set_strategy(apple, **update) -> None:
    group = apple.ad_groups[AD_GROUP]
    apple.ad_groups[AD_GROUP] = group.model_copy(update={
        "bid_strategy": group.bid_strategy.model_copy(update=update)})


def test_a_default_bid_apply_refuses_a_strategy_changed_since_the_preview() -> None:
    tools, apple, _ = rig(**ROOMY)
    preview = tools["preview_ad_group_default_bid"](ad_group_id=AD_GROUP, new_bid="1.10")
    # Someone moves the ad group onto Apple's bidding in the UI; the bid is untouched.
    _set_strategy(apple, bid_strategy_type=sdk.BidStrategyType("MAX_CONVERSIONS"),
                  bid_strategy_goal=sdk.BidStrategyGoal("INSTALL"))
    _raises(lambda: tools["apply_ad_group_default_bid"](preview_token=preview.preview_token),
            "Something changed it in between")
    assert apple.writes() == []
    assert apple.ad_groups[AD_GROUP].bid_strategy.bid_strategy_type.value == "MAX_CONVERSIONS"


def test_a_default_bid_apply_refuses_a_goal_changed_since_the_preview() -> None:
    tools, apple, _ = rig(**ROOMY)
    preview = tools["preview_ad_group_default_bid"](ad_group_id=AD_GROUP, new_bid="1.10")
    _set_strategy(apple, bid_strategy_goal=sdk.BidStrategyGoal("INSTALL"))
    _raises(lambda: tools["apply_ad_group_default_bid"](preview_token=preview.preview_token),
            "Something changed it in between")
    assert apple.writes() == []


def test_a_strategy_apply_refuses_a_default_bid_changed_since_the_preview() -> None:
    tools, apple, _ = rig(**ROOMY)
    preview = tools["preview_ad_group_bid_strategy"](
        ad_group_id=AD_GROUP, bid_strategy_type="MAX_CONVERSIONS", bid_strategy_goal="INSTALL")
    group = apple.ad_groups[AD_GROUP]
    _set_strategy(apple, bid=group.bid_strategy.bid.model_copy(update={"amount": "2.00"}))
    _raises(lambda: tools["apply_ad_group_bid_strategy"](preview_token=preview.preview_token),
            "Something changed it in between")
    assert apple.writes() == []
    assert apple.ad_groups[AD_GROUP].bid_strategy.bid.amount == "2.00"


def test_cpa_goal_bounds() -> None:
    tools, apple, _ = rig()
    over = tools["preview_ad_group_cpa_goal"](ad_group_id=AD_GROUP, cpa_goal="25.00")
    assert over.blocked and failed(over, "max_cpa_goal"), over.guardrail_checks
    first = tools["preview_ad_group_cpa_goal"](ad_group_id=AD_GROUP, cpa_goal="2.00")
    assert not first.blocked, first.guardrail_checks
    assert any("revert_change will refuse" in w for w in first.warnings)
    tools["apply_ad_group_cpa_goal"](preview_token=first.preview_token)
    assert apple.ad_groups[AD_GROUP].cpa_cap.value.amount == "2.00"
    jump = tools["preview_ad_group_cpa_goal"](ad_group_id=AD_GROUP, cpa_goal="4.00")
    assert jump.blocked and failed(jump, "max_cpa_goal_change_pct"), jump.guardrail_checks
    # +25% of a 10/day spend rate.
    step = tools["preview_ad_group_cpa_goal"](ad_group_id=AD_GROUP, cpa_goal="2.50")
    assert step.projected_daily_spend_delta == "2.50", step.projected_daily_spend_delta
    result = tools["apply_ad_group_cpa_goal"](preview_token=step.preview_token)
    undo = tools["revert_change"](entry_id=result.entry_id)
    assert undo.applied and apple.ad_groups[AD_GROUP].cpa_cap.value.amount == "2.00"


def test_ending_earlier_is_free_and_starting_earlier_is_an_enable() -> None:
    tools, apple, _ = rig(**ROOMY)
    apple.campaigns[CAMPAIGN] = apple.campaigns[CAMPAIGN].model_copy(update={
        "start_time": dt.datetime(2026, 12, 10),
        "end_time": dt.datetime(2027, 1, 31),
    })
    shorter = tools["preview_campaign_schedule"](campaign_id=CAMPAIGN, end_time="2027-01-15")
    assert shorter.projected_daily_spend_delta == "0" and not shorter.blocked, shorter
    assert any("STOPS serving" in w for w in shorter.warnings)
    earlier = tools["preview_campaign_schedule"](campaign_id=CAMPAIGN, start_time="2026-12-01")
    assert earlier.projected_daily_spend_delta == "100.00"
    assert any(c.startswith("SKIP projected_daily_delta") for c in earlier.guardrail_checks)
    result = tools["apply_campaign_schedule"](preview_token=shorter.preview_token)
    assert result.applied, result.failures
    assert apple.campaigns[CAMPAIGN].end_time.day == 15
    undo = tools["revert_change"](entry_id=result.entry_id)
    assert undo.applied and undo.projected_daily_spend_delta == "100.00"
    assert apple.campaigns[CAMPAIGN].end_time.day == 31


def test_a_campaign_with_no_strategy_cannot_be_reverted_to_none() -> None:
    tools, apple, _ = rig(**ROOMY)
    preview = tools["preview_campaign_bid_strategy"](campaign_id=CAMPAIGN, bid_strategy_type="MAX_CONVERSIONS",
                                                    bid_strategy_goal="INSTALL")
    assert preview.before == "unknown/-" and preview.projected_daily_spend_delta == "100.00"
    result = tools["apply_campaign_bid_strategy"](preview_token=preview.preview_token)
    assert result.applied
    _raises(lambda: tools["revert_change"](entry_id=result.entry_id), "cannot be written back")


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
