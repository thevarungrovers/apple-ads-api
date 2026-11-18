#!/usr/bin/env python3
"""The general low-risk tool: a closed allowlist that cannot reach money.

Run it without touching Apple:
    ./venv/bin/python tests/test_low_risk.py
"""

from __future__ import annotations

import pathlib
import sys
import typing
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import AD, AD_GROUP, CAMPAIGN, CREATIVE, rig, run  # noqa: E402

import apple_ads_platform as sdk  # noqa: E402

from apple_ads_mcp.changes.low_risk import ALLOWLIST, FORBIDDEN_FIELDS, RENAMEABLE  # noqa: E402

UPDATE_MODELS = {
    "campaign": sdk.CampaignUpdate,
    "ad_group": sdk.AdGroupUpdate,
    "ad": sdk.AdUpdate,
    "creative": sdk.CreativeUpdate,
}


def _mentions_money(annotation) -> bool:
    if annotation is sdk.Money:
        return True
    return any(_mentions_money(arg) for arg in typing.get_args(annotation))


def _raises(fn, needle: str) -> None:
    try:
        fn()
    except Exception as exc:
        assert needle in str(exc), f"expected {needle!r} in {exc!r}"
        return
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def test_the_allowlist_is_pinned() -> None:
    assert ALLOWLIST == frozenset({
        ("campaign", "name"), ("ad_group", "name"), ("ad", "name"), ("creative", "name"),
        ("daily_budget_recommendation", "dismiss"), ("target_cpa_recommendation", "dismiss"),
    })


def test_no_allowlisted_field_is_forbidden_or_money_typed() -> None:
    for entity_type, field in ALLOWLIST:
        assert field not in FORBIDDEN_FIELDS, (entity_type, field)
        model = UPDATE_MODELS.get(entity_type)
        if model is None:
            continue
        assert field in model.model_fields, f"{model.__name__} has no {field}"
        assert not _mentions_money(model.model_fields[field].annotation), (entity_type, field)


def test_every_money_typed_update_field_is_forbidden() -> None:
    for entity_type, model in UPDATE_MODELS.items():
        for name, info in model.model_fields.items():
            if _mentions_money(info.annotation) or name in ("daily_budget", "bid_strategy", "cpa_cap"):
                assert name in FORBIDDEN_FIELDS, f"{model.__name__}.{name} is not forbidden"
                assert (entity_type, name) not in ALLOWLIST


def test_anything_off_the_allowlist_is_refused() -> None:
    tools, _apple, _ = rig()
    for entity_type, field in (("campaign", "status"), ("campaign", "daily_budget"),
                               ("ad_group", "bid_strategy"), ("keyword", "name"),
                               ("ad_group", "targeting")):
        _raises(lambda: tools["preview_low_risk_change"](
            entity_type=entity_type, entity_id="1", field=field, value="x"), "not a low-risk edit")


def test_renames_round_trip() -> None:
    tools, apple, _ = rig()
    stores = {"campaign": (apple.campaigns, CAMPAIGN), "ad_group": (apple.ad_groups, AD_GROUP),
              "ad": (apple.ads, AD), "creative": (apple.creatives, CREATIVE)}
    assert set(stores) == set(RENAMEABLE)
    for entity_type, (store, entity_id) in stores.items():
        old = store[entity_id].name
        preview = tools["preview_low_risk_change"](
            entity_type=entity_type, entity_id=str(entity_id), field="name", value="Renamed Example")
        assert (preview.before, preview.after) == (old, "Renamed Example")
        assert preview.projected_daily_spend_delta == "0"
        result = tools["apply_low_risk_change"](preview_token=preview.preview_token)
        assert result.applied and store[entity_id].name == "Renamed Example", entity_type
        undo = tools["revert_change"](entry_id=result.entry_id)
        assert undo.applied and store[entity_id].name == old, entity_type


def test_a_dismissal_has_no_inverse() -> None:
    tools, apple, _ = rig()
    apple.extra["query_daily_budget_recommendations"] = lambda **_: SimpleNamespace(error=None, result=[
        sdk.DailyCapRecommendation.model_validate({
            "id": "rec-9", "campaignId": CAMPAIGN, "promotedObjectId": "1234567890",
            "promotedObjectType": "APPSTORE_APP"})])
    sent = []
    apple.extra["dismiss_daily_budget_recommendations"] = lambda **kw: sent.append(kw) or SimpleNamespace(
        error=None, result=None)
    preview = tools["preview_low_risk_change"](
        entity_type="daily_budget_recommendation", entity_id="rec-9", field="dismiss")
    assert any("NO inverse" in w for w in preview.warnings)
    result = tools["apply_low_risk_change"](preview_token=preview.preview_token)
    assert result.applied
    assert sent[0]["apply_daily_cap_recommendation"][0].id == "rec-9"
    _raises(lambda: tools["revert_change"](entry_id=result.entry_id), "cannot be brought back")


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
