#!/usr/bin/env python3
"""3d: creatives, ads (created PAUSED) and ad status.

Run it without touching Apple:
    ./venv/bin/python tests/test_kinds_ads.py
"""

from __future__ import annotations

import pathlib
import sys
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import AD, AD_GROUP, CREATIVE, rig, run  # noqa: E402

from apple_ads_mcp import ledger  # noqa: E402
from apple_ads_mcp.guardrails import DailyUsage  # noqa: E402
from kindkit import limits  # noqa: E402

ROOMY = dict(max_projected_session_delta=Decimal("500"), max_projected_delta_per_day_total=Decimal("500"))


def _raises(fn, needle: str) -> None:
    try:
        fn()
    except Exception as exc:
        assert needle in str(exc), f"expected {needle!r} in {exc!r}"
        return
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def test_a_creative_is_created_costs_three_and_cannot_be_reverted() -> None:
    tools, apple, state = rig()
    apple.extra["get_product_page_by_id"] = lambda **_: SimpleNamespace(
        result=SimpleNamespace(id="pp1", adam_id="1234567890", state="VISIBLE"), error=None)
    preview = tools["preview_create_creative"](name="Example CPP", adam_id="1234567890", product_page_id="pp1")
    assert not preview.blocked, preview.guardrail_checks
    assert preview.after.startswith("CUSTOM_PRODUCT_PAGE")
    result = tools["apply_create_creative"](preview_token=preview.preview_token)
    assert result.applied, result.failures
    new_id = int(result.entity_id)
    assert apple.creatives[new_id].name == "AB-max-Example CPP", "stamped with the identity"
    assert state.counters.snapshot()["applies_this_session"] == 3
    assert DailyUsage(limits()).usage()[0] == 3
    _raises(lambda: tools["revert_change"](entry_id=result.entry_id), "never deleted")


def test_a_creative_and_an_ad_are_named_for_each_identity() -> None:
    from apple_ads_mcp.naming import Identity

    combos = {
        ("AB", "max"): "AB-max-Spring", ("AB", ""): "AB-Spring",
        ("", "max"): "max-Spring", ("", ""): "Spring",
    }
    for (owner, agent), expected in combos.items():
        tools, apple, _ = rig(identity=Identity.from_values(owner, agent), **ROOMY)
        creative = tools["preview_create_creative"](name="Spring", adam_id="1234567890")
        assert creative.entity_name == expected and creative.naming["final"] == expected, creative
        result = tools["apply_create_creative"](preview_token=creative.preview_token)
        assert apple.creatives[int(result.entity_id)].name == expected
        ad = tools["preview_create_ad"](ad_group_id=AD_GROUP, creative_id=CREATIVE, name="Spring")
        assert ad.naming["final"] == expected and not ad.blocked, ad.guardrail_checks
        result = tools["apply_create_ad"](preview_token=ad.preview_token)
        new_id = int(ledger.find_entry(result.entry_id)["entity_id"])
        assert apple.ads[new_id].name == expected
        row = ledger.find_entry(result.entry_id)
        assert row["detail"]["requested_name"] == "Spring", row
        assert row["detail"]["identity"]["owner"] == (owner or None), row


def test_a_duplicate_ad_name_is_a_warning_not_a_failure() -> None:
    tools, apple, _ = rig(**ROOMY)
    apple.ads[AD] = apple.ads[AD].model_copy(update={"name": "AB-max-Spring"})
    ad = tools["preview_create_ad"](ad_group_id=AD_GROUP, creative_id=CREATIVE, name="Spring")
    assert not ad.blocked and any("duplicate name" in w for w in ad.warnings), ad.warnings


def test_a_creative_for_another_apps_page_is_blocked() -> None:
    tools, apple, _ = rig()
    apple.extra["get_product_page_by_id"] = lambda **_: SimpleNamespace(
        result=SimpleNamespace(id="pp1", adam_id="999", state="VISIBLE"), error=None)
    preview = tools["preview_create_creative"](name="X", adam_id="1234567890", product_page_id="pp1")
    assert preview.blocked


def test_an_ad_is_created_paused_and_reverting_pauses_it() -> None:
    tools, apple, _ = rig(**ROOMY)
    preview = tools["preview_create_ad"](ad_group_id=AD_GROUP, creative_id=CREATIVE, name="Example Ad 2")
    assert preview.projected_daily_spend_delta == "0" and not preview.blocked, preview.guardrail_checks
    result = tools["apply_create_ad"](preview_token=preview.preview_token)
    new_id = int(ledger.find_entry(result.entry_id)["entity_id"])
    assert apple.ads[new_id].status.value == "PAUSED"
    _raises(lambda: tools["revert_change"](entry_id=result.entry_id), "already PAUSED")
    enable = tools["preview_ad_status"](ad_id=new_id, status="ENABLED")
    assert enable.projected_daily_spend_delta == "90.00"
    tools["apply_ad_status"](preview_token=enable.preview_token)
    undo = tools["revert_change"](entry_id=result.entry_id)
    assert undo.applied and apple.ads[new_id].status.value == "PAUSED"


def test_pausing_an_ad_uses_its_own_report_row() -> None:
    tools, apple, _ = rig()
    apple.reports["apps_ad_reports"] = [apple.report_row(0, spend="14.00", taps=14, adId=AD)]
    preview = tools["preview_ad_status"](ad_id=AD, status="PAUSED")
    assert preview.projected_daily_spend_delta == "-2.00", preview.projected_daily_spend_delta


def test_a_missing_creative_blocks_the_ad() -> None:
    tools, _apple, _ = rig()
    preview = tools["preview_create_ad"](ad_group_id=AD_GROUP, creative_id=424242, name="X")
    assert preview.blocked


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
