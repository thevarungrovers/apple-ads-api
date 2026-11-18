#!/usr/bin/env python3
"""3e: creating campaigns and ad groups -- always PAUSED, 3 applies each.

Run it without touching Apple:
    ./venv/bin/python tests/test_kinds_creation.py
"""

from __future__ import annotations

import inspect
import pathlib
import sys
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import APP, CAMPAIGN, failed, rig, run  # noqa: E402

from apple_ads_mcp import ledger  # noqa: E402

ROOMY = dict(max_projected_session_delta=Decimal("500"), max_projected_delta_per_day_total=Decimal("500"))


def _raises(fn, needle: str) -> None:
    try:
        fn()
    except Exception as exc:
        assert needle in str(exc), f"expected {needle!r} in {exc!r}"
        return
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def _campaign_args(**over):
    args = dict(name="Example Launch", adam_id=APP, countries=["US"], daily_budget="40.00",
                regulation_responses=[])
    args.update(over)
    return args


def test_regulation_responses_is_required_with_no_default() -> None:
    tools, _apple, _ = rig()
    parameter = inspect.signature(tools["preview_create_campaign"]).parameters["regulation_responses"]
    assert parameter.default is inspect.Parameter.empty


def test_a_campaign_is_created_paused_and_shows_its_declaration() -> None:
    tools, apple, state = rig(**ROOMY)
    preview = tools["preview_create_campaign"](**_campaign_args(
        regulation_responses=[{"regulation_type": "CAMPAIGN_SAPIN_LAW", "response_value": "NOT_AGENT"}]
    ))
    assert not preview.blocked, preview.guardrail_checks
    assert "CAMPAIGN_SAPIN_LAW = NOT_AGENT" in preview.after and "PAUSED" in preview.after
    assert preview.projected_daily_spend_delta == "0"
    result = tools["apply_create_campaign"](preview_token=preview.preview_token)
    assert result.applied, result.failures
    created = apple.campaigns[int(result.entity_id)]
    assert created.status.value == "PAUSED" and created.daily_budget.value.amount == "40.00"
    assert created.regulation_responses[0].response_value.value == "NOT_AGENT"
    assert state.counters.snapshot()["applies_this_session"] == 3
    _raises(lambda: tools["revert_change"](entry_id=result.entry_id), "already PAUSED")


def test_a_campaign_over_the_budget_ceiling_is_blocked() -> None:
    tools, _apple, _ = rig()
    preview = tools["preview_create_campaign"](**_campaign_args(daily_budget="500.00"))
    assert preview.blocked and failed(preview, "max_daily_budget"), preview.guardrail_checks


def test_an_ineligible_app_is_blocked() -> None:
    tools, apple, _ = rig()
    apple._eligibilities_apps_query_post = lambda **_: SimpleNamespace(
        result=[], error=None)
    preview = tools["preview_create_campaign"](**_campaign_args())
    assert preview.blocked and failed(preview, "app_eligible"), preview.guardrail_checks


def test_an_ad_group_is_created_paused_with_its_bids() -> None:
    tools, apple, _ = rig(**ROOMY)
    preview = tools["preview_create_ad_group"](
        campaign_id=CAMPAIGN, name="Example Group", default_bid="1.50", cpa_goal="5.00",
        devices=["IPHONE"],
    )
    assert not preview.blocked, preview.guardrail_checks
    result = tools["apply_create_ad_group"](preview_token=preview.preview_token)
    group = apple.ad_groups[int(ledger.find_entry(result.entry_id)["entity_id"])]
    assert group.status.value == "PAUSED"
    assert group.bid_strategy.bid.amount == "1.50" and group.cpa_cap.value.amount == "5.00"
    assert group.targeting.device_class.include == ["IPHONE"]
    over = tools["preview_create_ad_group"](campaign_id=CAMPAIGN, name="X", default_bid="9.00")
    assert over.blocked and failed(over, "max_bid")


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
