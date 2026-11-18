#!/usr/bin/env python3
"""Nothing that can increase spend projects 0 -- Fix 1.4.

Enabling a campaign, ad group or keyword, pausing a negative keyword and
reverting a pause or a budget cut all project a real upper bound. An enable is
exempt from the per-change limit and counted, in full, against the daily total.

Run it without touching Apple:
    ./venv/bin/python tests/test_projections.py
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
from decimal import Decimal

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import AD_GROUP, CAMPAIGN, KEYWORD, NEGATIVE, limits, rig, run  # noqa: E402

import apple_ads_platform as sdk  # noqa: E402

from apple_ads_mcp import ledger  # noqa: E402
from apple_ads_mcp.guardrails import DailyUsage, limit_warnings  # noqa: E402


def _pause_campaign(apple) -> None:
    apple.campaigns[CAMPAIGN] = apple.campaigns[CAMPAIGN].model_copy(
        update={"status": sdk.CampaignStatus("PAUSED")}
    )


def _used_today(amount: str) -> None:
    ledger.write_intent(
        "earlier", tool="apply_campaign_daily_budget", entity_type="campaign",
        entity_id=CAMPAIGN, entity_name="x", path="p", field="daily_budget", before="40",
        after="100", campaign_id=CAMPAIGN, projected_daily_spend_delta=amount,
    )
    ledger.write_outcome("earlier", outcome="applied")


def _line(preview, name: str) -> str:
    return next(c for c in preview.guardrail_checks if f" {name}:" in c)


def test_re_enabling_a_campaign_projects_its_whole_budget_and_skips_the_per_change_limit() -> None:
    tools, apple, _ = rig(max_projected_delta_per_day_total=Decimal("150"))
    _pause_campaign(apple)
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN, status="ENABLED")
    assert preview.projected_daily_spend_delta == "100.00", preview.projected_daily_spend_delta
    assert _line(preview, "projected_daily_delta").startswith("SKIP"), preview.guardrail_checks
    assert not preview.blocked, preview.guardrail_checks
    assert any("re-enabling resumes spend" in w for w in preview.warnings), preview.warnings
    result = tools["apply_campaign_status"](preview_token=preview.preview_token)
    assert result.applied
    assert DailyUsage(limits()).usage()[1] == Decimal("100.00")


def test_re_enabling_is_refused_once_the_day_is_too_spent() -> None:
    tools, apple, _ = rig(max_projected_delta_per_day_total=Decimal("150"))
    _pause_campaign(apple)
    _used_today("60.00")
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN, status="ENABLED")
    line = _line(preview, "projected_daily_total")
    assert preview.blocked and line.startswith("FAIL"), preview.guardrail_checks
    assert "100.00" in line and "90.00" in line and "60.00" in line, line


def test_enabling_a_keyword_or_ad_group_projects_the_campaign_headroom() -> None:
    tools, apple, _ = rig()
    apple.keywords[KEYWORD] = apple.keywords[KEYWORD].model_copy(
        update={"status": sdk.KeywordStatus("PAUSED")}
    )
    apple.ad_groups[AD_GROUP] = apple.ad_groups[AD_GROUP].model_copy(
        update={"status": sdk.AdGroupStatus("PAUSED")}
    )
    # Budget 100, 70 spent over 7 days -> 10/day used, 90 of headroom.
    for name, args in (("keyword_status", {"keyword_id": KEYWORD}),
                       ("ad_group_status", {"ad_group_id": AD_GROUP})):
        preview = tools[f"preview_{name}"](status="ENABLED", **args)
        assert preview.projected_daily_spend_delta == "90.00", (name, preview.projected_daily_spend_delta)
        assert _line(preview, "projected_daily_delta").startswith("SKIP")


def test_enabling_under_a_paused_campaign_projects_zero_with_a_reason() -> None:
    tools, apple, _ = rig()
    _pause_campaign(apple)
    apple.keywords[KEYWORD] = apple.keywords[KEYWORD].model_copy(
        update={"status": sdk.KeywordStatus("PAUSED")}
    )
    preview = tools["preview_keyword_status"](keyword_id=KEYWORD, status="ENABLED")
    assert preview.projected_daily_spend_delta == "0", preview.projected_daily_spend_delta
    assert "campaign is PAUSED" in _line(preview, "spend_is_bounded")


def test_pausing_a_negative_projects_the_headroom() -> None:
    tools, _apple, _ = rig()
    preview = tools["preview_negative_keyword_pause"](negative_keyword_id=NEGATIVE, status="PAUSED")
    assert preview.projected_daily_spend_delta == "90.00", preview.projected_daily_spend_delta
    assert _line(preview, "projected_daily_delta").startswith("SKIP")


def test_pausing_never_consumes_the_cap() -> None:
    tools, _apple, _ = rig()
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN, status="PAUSED")
    assert Decimal(preview.projected_daily_spend_delta) < 0
    tools["apply_campaign_status"](preview_token=preview.preview_token)
    assert DailyUsage(limits()).usage() == (1, Decimal("0"))


def test_reverting_a_pause_and_a_budget_cut_restore_spend() -> None:
    tools, _apple, state = rig(max_projected_session_delta=Decimal("500"),
                               max_projected_delta_per_day_total=Decimal("500"))
    pause = tools["preview_campaign_status"](campaign_id=CAMPAIGN, status="PAUSED")
    paused = tools["apply_campaign_status"](preview_token=pause.preview_token)
    undo = tools["revert_change"](entry_id=paused.entry_id)
    assert undo.projected_daily_spend_delta == "100.00", undo.projected_daily_spend_delta

    cut = tools["preview_campaign_daily_budget"](campaign_id=CAMPAIGN, new_daily_budget="90.00")
    applied = tools["apply_campaign_daily_budget"](preview_token=cut.preview_token)
    restore = tools["revert_change"](entry_id=applied.entry_id)
    assert restore.projected_daily_spend_delta == "10.00", restore.projected_daily_spend_delta
    assert Decimal(state.counters.snapshot()["projected_session_delta"]) == Decimal("110.00")


def test_a_revert_that_restores_spend_is_refused_when_the_day_is_spent() -> None:
    tools, _apple, _ = rig(max_projected_delta_per_day_total=Decimal("150"))
    pause = tools["preview_campaign_status"](campaign_id=CAMPAIGN, status="PAUSED")
    paused = tools["apply_campaign_status"](preview_token=pause.preview_token)
    _used_today("60.00")
    try:
        tools["revert_change"](entry_id=paused.entry_id)
    except Exception as exc:  # ToolError
        assert "daily spend limit" in str(exc) and "100.00" in str(exc), exc
    else:
        raise AssertionError("a revert restoring 100/day went past a spent day")


def test_limits_that_make_a_re_enable_impossible_are_reported() -> None:
    assert limit_warnings(limits(max_projected_delta_per_day_total=Decimal("100")))
    assert not limit_warnings(limits(max_projected_delta_per_day_total=Decimal("200")))


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
