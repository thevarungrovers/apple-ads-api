#!/usr/bin/env python3
"""3c: targeting. Widening is counted like an enable; narrowing projects 0.

Run it without touching Apple:
    ./venv/bin/python tests/test_kinds_targeting.py
"""

from __future__ import annotations

import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import AD_GROUP, CAMPAIGN, rig, run  # noqa: E402

import apple_ads_platform as sdk  # noqa: E402

from apple_ads_mcp.changes.targeting import daypart_grid, widens  # noqa: E402

ROOMY = dict(max_projected_session_delta=Decimal("500"), max_projected_delta_per_day_total=Decimal("500"))


def _raises(fn, needle: str) -> None:
    try:
        fn()
    except Exception as exc:
        assert needle in str(exc), f"expected {needle!r} in {exc!r}"
        return
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def test_narrowing_devices_projects_zero_and_round_trips() -> None:
    tools, apple, _ = rig(**ROOMY)
    preview = tools["preview_ad_group_devices"](ad_group_id=AD_GROUP, devices=["IPHONE"])
    assert preview.projected_daily_spend_delta == "0"
    assert preview.before == "device_class: IPAD, IPHONE" and preview.after == "device_class: IPHONE"
    result = tools["apply_ad_group_devices"](preview_token=preview.preview_token)
    assert result.applied, result.failures
    assert apple.ad_groups[AD_GROUP].targeting.device_class.include == ["IPHONE"]
    undo = tools["revert_change"](entry_id=result.entry_id)
    assert undo.applied and undo.projected_daily_spend_delta == "90.00"
    assert sorted(apple.ad_groups[AD_GROUP].targeting.device_class.include) == ["IPAD", "IPHONE"]


def test_widening_is_an_enable() -> None:
    tools, apple, _ = rig(**ROOMY)
    group = apple.ad_groups[AD_GROUP]
    apple.ad_groups[AD_GROUP] = group.model_copy(update={"targeting": sdk.AdGroupTargeting.model_validate(
        {"deviceClass": {"include": ["IPHONE"]}, "minAge": {"include": ["25"]}})})
    devices = tools["preview_ad_group_devices"](ad_group_id=AD_GROUP, devices=["IPHONE", "IPAD"])
    assert devices.projected_daily_spend_delta == "90.00"
    assert any(c.startswith("SKIP projected_daily_delta") for c in devices.guardrail_checks)
    ages = tools["preview_ad_group_demographics"](ad_group_id=AD_GROUP, min_age=18)
    assert ages.projected_daily_spend_delta == "90.00"
    stricter = tools["preview_ad_group_demographics"](ad_group_id=AD_GROUP, min_age=30, genders=["F"])
    assert stricter.projected_daily_spend_delta == "0"


def test_search_match_on_is_an_enable_and_off_is_free() -> None:
    tools, apple, _ = rig(**ROOMY)
    on = tools["preview_ad_group_search_match"](ad_group_id=AD_GROUP, enabled=True)
    assert (on.before, on.after, on.projected_daily_spend_delta) == ("off", "on", "90.00")
    result = tools["apply_ad_group_search_match"](preview_token=on.preview_token)
    assert result.applied and apple.ad_groups[AD_GROUP].automated_keywords_opt_in is True
    _raises(lambda: tools["preview_ad_group_search_match"](ad_group_id=AD_GROUP, enabled=True), "already on")
    off = tools["preview_ad_group_search_match"](ad_group_id=AD_GROUP, enabled=False)
    assert off.projected_daily_spend_delta == "0"


def test_daypart_renders_a_grid() -> None:
    tools, _apple, _ = rig(**ROOMY)
    preview = tools["preview_ad_group_daypart"](ad_group_id=AD_GROUP, hours=[24 + 9, 24 + 10])
    grid = next(w for w in preview.warnings if w.startswith("Serves in"))
    assert "Mon  .........##" in grid, grid
    _raises(lambda: tools["preview_ad_group_daypart"](ad_group_id=AD_GROUP, hours=[200]), "0 and 167")
    assert daypart_grid([]) == "every hour of every day"


def test_locations_and_app_downloaders() -> None:
    tools, apple, _ = rig(**ROOMY)
    preview = tools["preview_ad_group_locations"](ad_group_id=AD_GROUP, localities=["US|NY|Springfield"])
    assert preview.projected_daily_spend_delta == "0"  # adding a first restriction narrows
    tools["apply_ad_group_locations"](preview_token=preview.preview_token)
    assert apple.ad_groups[AD_GROUP].targeting.locality.include == ["US|NY|Springfield"]
    new_users = tools["preview_ad_group_app_downloaders"](ad_group_id=AD_GROUP, exclude_app_ids=["1234567890"])
    assert new_users.projected_daily_spend_delta == "0"
    tools["apply_ad_group_app_downloaders"](preview_token=new_users.preview_token)
    assert apple.ad_groups[AD_GROUP].targeting.app_downloader.exclude == ["1234567890"]
    back = tools["preview_ad_group_app_downloaders"](ad_group_id=AD_GROUP)
    assert back.projected_daily_spend_delta == "90.00", "dropping an exclusion widens"


def test_adding_a_country_widens_and_says_what_it_implies() -> None:
    tools, apple, _ = rig(**ROOMY)
    preview = tools["preview_campaign_countries"](campaign_id=CAMPAIGN, countries=["US", "FR"])
    assert preview.projected_daily_spend_delta == "90.00"
    assert any("storefront" in w for w in preview.warnings)
    assert any("Sapin" in w for w in preview.warnings)
    result = tools["apply_campaign_countries"](preview_token=preview.preview_token)
    assert result.applied
    assert sorted(apple.campaigns[CAMPAIGN].targeting.country_or_region.include) == ["FR", "US"]


def test_widening_rules() -> None:
    dim = lambda inc=(), exc=(): {"include": list(inc), "exclude": list(exc)}  # noqa: E731
    assert widens({"device_class": dim(["IPHONE"])}, {"device_class": dim([])})
    assert not widens({"device_class": dim([])}, {"device_class": dim(["IPHONE"])})
    assert widens({"app_downloader": dim([], ["1"])}, {"app_downloader": dim([], [])})
    assert widens({"max_age": dim(["40"])}, {"max_age": dim(["50"])})
    assert not widens({"max_age": dim(["40"])}, {"max_age": dim(["30"])})


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
