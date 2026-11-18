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


def _stores(apple):
    return {"campaign": (apple.campaigns, CAMPAIGN), "ad_group": (apple.ad_groups, AD_GROUP),
            "ad": (apple.ads, AD), "creative": (apple.creatives, CREATIVE)}


def _name(store, entity_id, name) -> None:
    store[entity_id] = store[entity_id].model_copy(update={"name": name})


def test_renames_round_trip() -> None:
    """Our own entity (it carries our prefix): only the base changes, the prefix
    is kept, and a revert restores the exact old name without re-stamping."""
    tools, apple, _ = rig()
    stores = _stores(apple)
    assert set(stores) == set(RENAMEABLE)
    for entity_type, (store, entity_id) in stores.items():
        _name(store, entity_id, "AB-max-Example")
        old = store[entity_id].name
        preview = tools["preview_low_risk_change"](
            entity_type=entity_type, entity_id=str(entity_id), field="name", value="Renamed Example")
        assert (preview.before, preview.after) == (old, "AB-max-Renamed Example")
        assert preview.naming["rule"].startswith("rename of our own entity"), preview.naming
        assert preview.projected_daily_spend_delta == "0"
        result = tools["apply_low_risk_change"](preview_token=preview.preview_token)
        assert result.applied and store[entity_id].name == "AB-max-Renamed Example", entity_type
        undo = tools["revert_change"](entry_id=result.entry_id)
        assert undo.applied and store[entity_id].name == old, entity_type


def _refused(fn, needle: str) -> None:
    try:
        fn()
    except Exception as exc:
        assert needle in str(exc), f"expected {needle!r} in {exc!r}"
        return
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def test_a_manual_or_foreign_entity_cannot_be_renamed() -> None:
    tools, apple, _ = rig()
    for entity_type, (store, entity_id) in _stores(apple).items():
        # A hand-made entity (no prefix, no ledger row): refused, no override.
        _refused(lambda: tools["preview_low_risk_change"](
            entity_type=entity_type, entity_id=str(entity_id), field="name", value="X"),
            "not this agent's")
        _name(store, entity_id, "CD-bot-Example")
        _refused(lambda: tools["preview_low_risk_change"](
            entity_type=entity_type, entity_id=str(entity_id), field="name", value="X"),
            "CD-bot-")
        # Our own, but asking to swap in another prefix: refused too.
        _name(store, entity_id, "AB-max-Example")
        _refused(lambda: tools["preview_low_risk_change"](
            entity_type=entity_type, entity_id=str(entity_id), field="name", value="CD-bot-X"),
            "another agent's prefix")
    assert apple.writes() == []


def test_a_ledger_created_entity_that_lost_its_prefix_gets_it_back() -> None:
    from decimal import Decimal

    from apple_ads_mcp import ledger
    from kindkit import APP

    tools, apple, _ = rig(max_projected_session_delta=Decimal("500"),
                          max_projected_delta_per_day_total=Decimal("500"))
    created = tools["preview_create_campaign"](
        name="Launch", adam_id=APP, countries=["US"], daily_budget="40.00", regulation_responses=[])
    result = tools["apply_create_campaign"](preview_token=created.preview_token)
    new_id = int(result.entity_id)
    _name(apple.campaigns, new_id, "Launch renamed in the UI")
    preview = tools["preview_low_risk_change"](
        entity_type="campaign", entity_id=str(new_id), field="name", value="Launch 2")
    assert preview.after == "AB-max-Launch 2" and "restoring" in preview.naming["rule"]
    applied = tools["apply_low_risk_change"](preview_token=preview.preview_token)
    assert applied.applied and apple.campaigns[new_id].name == "AB-max-Launch 2"
    row = ledger.find_entry(applied.entry_id)
    assert row["detail"]["requested_name"] == "Launch 2" and row["detail"]["identity"]["agent"] == "max"


def test_renames_for_each_identity() -> None:
    from apple_ads_mcp import ledger
    from apple_ads_mcp.naming import Identity

    combos = {("AB", "max"): "AB-max-", ("AB", ""): "AB-", ("", "max"): "max-", ("", ""): ""}
    for (owner, agent), prefix in combos.items():
        tools, apple, _ = rig(identity=Identity.from_values(owner, agent))
        if prefix:
            _name(apple.ad_groups, AD_GROUP, prefix + "Old")
        else:
            # No prefix to recognise it by: only the ledger can say it is ours.
            ledger.write_intent("seed", tool="apply_create_ad_group", entity_type="ad_group",
                                entity_id=AD_GROUP, entity_name="Old", path="-", field="created",
                                before="(none)", after="PAUSED", campaign_id=CAMPAIGN,
                                projected_daily_spend_delta="0")
            ledger.write_outcome("seed", outcome=ledger.APPLIED)
        preview = tools["preview_low_risk_change"](
            entity_type="ad_group", entity_id=str(AD_GROUP), field="name", value="New")
        assert preview.after == prefix + "New", ((owner, agent), preview.after)
        result = tools["apply_low_risk_change"](preview_token=preview.preview_token)
        assert result.applied and apple.ad_groups[AD_GROUP].name == prefix + "New"
        # The same name again is "unchanged", judged on the final name.
        _refused(lambda: tools["preview_low_risk_change"](
            entity_type="ad_group", entity_id=str(AD_GROUP), field="name", value=prefix + "New"),
            "already called")


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
