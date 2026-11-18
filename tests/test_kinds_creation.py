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


def test_a_campaign_and_an_ad_group_are_named_for_each_identity() -> None:
    from apple_ads_mcp.naming import Identity

    combos = {
        ("AB", "max"): "AB-max-Example Launch", ("AB", ""): "AB-Example Launch",
        ("", "max"): "max-Example Launch", ("", ""): "Example Launch",
    }
    for (owner, agent), expected in combos.items():
        tools, apple, _ = rig(identity=Identity.from_values(owner, agent), **ROOMY)
        preview = tools["preview_create_campaign"](**_campaign_args())
        assert not preview.blocked, preview.guardrail_checks
        assert preview.entity_name == expected and preview.path == f"Campaign {expected!r}"
        assert preview.naming["final"] == expected and preview.naming["requested"] == "Example Launch"
        assert (expected != "Example Launch") == any(expected in w for w in preview.warnings)
        result = tools["apply_create_campaign"](preview_token=preview.preview_token)
        assert apple.campaigns[int(result.entity_id)].name == expected, "what was SENT"
        group = tools["preview_create_ad_group"](campaign_id=CAMPAIGN, name="Example Launch",
                                                 default_bid="1.50")
        assert group.naming["final"] == expected and not group.blocked, group.guardrail_checks
        result = tools["apply_create_ad_group"](preview_token=group.preview_token)
        new_id = int(ledger.find_entry(result.entry_id)["entity_id"])
        assert apple.ad_groups[new_id].name == expected


def test_our_prefix_is_not_doubled_and_a_foreign_one_is_refused() -> None:
    tools, _apple, _ = rig()
    again = tools["preview_create_campaign"](**_campaign_args(name="AB-max-Example Launch"))
    assert again.naming["final"] == "AB-max-Example Launch"
    _raises(lambda: tools["preview_create_campaign"](**_campaign_args(name="CD-bot-Example")),
            "another agent's prefix")


def test_a_malformed_identity_blocks_a_creation_naming_the_env_key() -> None:
    from apple_ads_mcp.naming import Identity

    tools, _apple, _ = rig(identity=Identity.from_values("a1", "max"))
    preview = tools["preview_create_campaign"](**_campaign_args())
    assert preview.blocked and failed(preview, "agent_identity"), preview.guardrail_checks
    assert any("APPLE_ADS_OWNER_INITIALS" in c for c in preview.guardrail_checks)


def test_the_length_cap_counts_the_prefix() -> None:
    tools, _apple, _ = rig()
    fits = tools["preview_create_campaign"](**_campaign_args(name="x" * 193))
    assert not fits.blocked and fits.naming["length"] == 200, fits.guardrail_checks
    over = tools["preview_create_campaign"](**_campaign_args(name="x" * 194))
    assert over.blocked and failed(over, "name_length"), over.guardrail_checks
    assert over.entity_name == "AB-max-" + "x" * 194, "shown in full, never truncated"


def test_a_duplicate_campaign_or_ad_group_name_fails_name_is_free() -> None:
    tools, apple, _ = rig()
    apple.campaigns[CAMPAIGN] = apple.campaigns[CAMPAIGN].model_copy(
        update={"name": "AB-max-Example Launch"})
    preview = tools["preview_create_campaign"](**_campaign_args())
    assert preview.blocked and failed(preview, "name_is_free"), preview.guardrail_checks
    from kindkit import AD_GROUP

    apple.ad_groups[AD_GROUP] = apple.ad_groups[AD_GROUP].model_copy(update={"name": "AB-max-Group"})
    group = tools["preview_create_ad_group"](campaign_id=CAMPAIGN, name="group", default_bid="1.50")
    assert group.blocked and failed(group, "name_is_free"), group.guardrail_checks
    other = tools["preview_create_ad_group"](campaign_id=CAMPAIGN, name="Group 2", default_bid="1.50")
    assert not failed(other, "name_is_free"), other.guardrail_checks


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
