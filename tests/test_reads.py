#!/usr/bin/env python3
"""Read tools against a FakeApple: reconcile, pagination, reports, lookups.

Run it without touching Apple:
    ./venv/bin/python tests/test_reads.py
"""

from __future__ import annotations

import datetime as dt
import pathlib
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import AD_GROUP, CAMPAIGN, KEYWORD, NEGATIVE, Registrar, rig, run  # noqa: E402

from apple_ads_mcp import tools_read  # noqa: E402


def read_tools(state):
    registrar = Registrar()
    tools_read.register(registrar, state)
    return registrar.tools


def _audit(entity_type: str, entity_id: str):
    return SimpleNamespace(
        transaction_id=f"t{entity_id}", event_type=None,
        event_time=dt.datetime.now(dt.timezone.utc), entity_type=entity_type,
        metas=[{entity_type: entity_id, "detailId": f"{entity_type}.{entity_id}.t"}],
        count=1, modified_by="api", user_type=None,
    )


def test_reconcile_joins_created_negatives_by_their_new_ids() -> None:
    tools, apple, state = rig()
    preview = tools["preview_negative_keywords_add"](texts=["one", "two"], ad_group_id=AD_GROUP)
    result = tools["apply_negative_keywords_add"](preview_token=preview.preview_token)
    created = [line.rsplit(" ", 1)[-1].rstrip(")") for line in result.results]

    def audit(audit_query, **_):
        entity_type = next(f.value for f in audit_query.filters if f.var_field == "entityType")
        rows = [_audit("NegativeKeyword", c) for c in created] if entity_type == "NegativeKeyword" else []
        return SimpleNamespace(result=rows, error=None)

    apple.extra["query_audit_summary"] = audit
    report = read_tools(state)["reconcile_ledger"](days=1)
    assert report.local_without_apple == [], report.local_without_apple
    assert report.apple_without_local == [], report.apple_without_local
    assert report.apple_truncated == []


def test_a_full_audit_page_is_reported_as_truncated() -> None:
    _tools, apple, state = rig()

    def audit(audit_query, **_):
        size = audit_query.pagination.page_size
        return SimpleNamespace(result=[_audit("Campaign", str(i)) for i in range(size)], error=None)

    apple.extra["query_audit_summary"] = audit
    listed = read_tools(state)["list_recent_changes"](days=1, limit=5)
    assert listed.truncated and listed.returned == 5


def test_lists_page_and_say_when_more_exist() -> None:
    _tools, apple, state = rig()
    for offset in range(1, 5):
        apple.keywords[KEYWORD + offset] = apple.keywords[KEYWORD].model_copy(
            update={"id": KEYWORD + offset}
        )
    tools = read_tools(state)
    first = tools["list_keywords"](ad_group_id=AD_GROUP, limit=2)
    assert first.returned == 2 and first.total == 5 and first.next_offset == 2 and first.truncated
    last = tools["list_keywords"](ad_group_id=AD_GROUP, limit=2, offset=4)
    assert last.returned == 1 and last.next_offset is None and not last.truncated


def test_negatives_report_their_real_total() -> None:
    _tools, apple, state = rig()
    for offset in range(1, 4):
        apple.negatives[NEGATIVE + offset] = apple.negatives[NEGATIVE].model_copy(
            update={"id": NEGATIVE + offset}
        )
    listed = read_tools(state)["list_negative_keywords"](campaign_id=CAMPAIGN, limit=2)
    assert listed.total == 4 and listed.returned == 2 and listed.next_offset == 2


def test_a_report_narrowed_client_side_reads_every_page() -> None:
    from apple_ads_mcp import tools_read as tr

    _tools, apple, state = rig()
    tr.REPORT_PAGE, saved = 2, tr.REPORT_PAGE
    try:
        apple.reports["apps_keyword_reports"] = [
            apple.report_row(KEYWORD + i, spend=f"{i}.00", taps=i, adGroupId=AD_GROUP)
            for i in range(1, 6)
        ]
        sent = []

        def paged(method, /, **kwargs):
            request = kwargs.get("apps_reporting_request")
            if request is not None:
                sent.append(request.pagination.offset)
                rows = apple.reports["apps_keyword_reports"]
                start = request.pagination.offset
                chunk = rows[start: start + request.pagination.page_size]
                import json
                return type("R", (), {"data": json.dumps(
                    {"result": {"rows": chunk}, "pagination": {"totalResults": len(rows)}}).encode()})()
            return apple(method, **kwargs)

        from apple_ads_mcp import client
        client.call = paged
        report = read_tools(state)["keyword_report"](campaign_id=CAMPAIGN, keyword_id=KEYWORD + 5)
        assert [r.id for r in report.rows] == [str(KEYWORD + 5)], report.rows
        assert sent == [0, 2, 4], sent
    finally:
        tr.REPORT_PAGE = saved


def test_a_report_page_says_how_many_rows_exist() -> None:
    _tools, apple, state = rig()
    apple.reports["apps_campaign_reports"] = [
        apple.report_row(CAMPAIGN + i, spend="1.00", taps=1) for i in range(3)
    ]
    report = read_tools(state)["campaign_report"](limit=3)
    assert report.total_rows_available == 3 and report.next_offset is None and not report.truncated


def test_report_windows_use_the_account_timezone() -> None:

    _tools, apple, state = rig()
    def account(id, **_):
        import apple_ads_platform as sdk
        return SimpleNamespace(result=sdk.AdAccount.model_validate(
            {"id": int(id), "timezone": "Pacific/Kiritimati", "currency": "USD"}), error=None)
    apple._ad_accounts_id_get = account
    report = read_tools(state)["campaign_report"](days=1)
    today_there = dt.datetime.now(ZoneInfo("Pacific/Kiritimati")).date().isoformat()
    assert report.end == today_there and report.time_zone == "Pacific/Kiritimati", report


def test_projections_use_seven_complete_days_ending_yesterday() -> None:
    tools, apple, _ = rig()
    tools["preview_campaign_status"](campaign_id=CAMPAIGN, status="PAUSED")
    request = next(k["apps_reporting_request"] for m, k in apple.calls
                   if m.endswith("_without_preload_content"))
    today = dt.datetime.now(ZoneInfo("America/New_York")).date()
    assert request.time_range.end == today - dt.timedelta(days=1), request.time_range
    assert request.time_range.start == today - dt.timedelta(days=7), request.time_range


def test_an_account_in_another_currency_blocks_every_preview() -> None:
    tools, _apple, _ = rig(allowed_currencies=frozenset({"CAD"}))
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN, status="PAUSED")
    assert preview.blocked
    assert any(c.startswith("FAIL account_currency") for c in preview.guardrail_checks), preview.guardrail_checks


def _acls(roles):
    account = SimpleNamespace(id="1234567890", name="Example Account", org_id=1)
    return SimpleNamespace(result=[SimpleNamespace(ad_account=account, roles=roles)], error=None)


def test_get_account_reports_timezone_currency_and_role() -> None:
    from apple_ads_mcp import tools_lookup

    _tools, apple, state = rig()
    apple.extra["get_user_acls"] = lambda **_: _acls(["API Account Read Only"])
    registrar = Registrar()
    tools_lookup.register(registrar, state)
    account = registrar.tools["get_account"]()
    assert account.timezone == "America/New_York" and account.currency == "USD"
    assert account.roles == ["API Account Read Only"] and account.role_can_write is False


def test_role_names_decide_whether_a_role_can_write() -> None:
    from apple_ads_mcp.tools_lookup import role_can_write

    assert role_can_write([]) is None
    assert role_can_write(["API Account Read Only"]) is False
    assert role_can_write(["API Account Read Write"]) is True
    assert role_can_write(["API Account Read Only", "API Campaign Manager"]) is True


def test_suggestions_are_filtered_by_app_campaign_and_country() -> None:
    from apple_ads_mcp import tools_lookup

    _tools, apple, state = rig()
    seen = {}

    def suggest(recommendation_query_request, **_):
        seen["filters"] = {f.var_field: f.value for f in recommendation_query_request.filters}
        return SimpleNamespace(result=[{"text": "example term"}], error=None, pagination=None)

    apple.extra["query_keyword_suggestions"] = suggest
    registrar = Registrar()
    tools_lookup.register(registrar, state)
    rows = registrar.tools["keyword_suggestions"](adam_id="1234567890", country="us")
    assert seen["filters"] == {"adamId": ["1234567890"], "countryOrRegion": ["us"]}, seen
    assert rows.rows == [{"text": "example term"}]


def test_shared_budgets_list_the_campaigns_they_feed() -> None:
    import apple_ads_platform as sdk
    from apple_ads_mcp import tools_lookup

    _tools, apple, state = rig()
    apple.campaigns[CAMPAIGN] = apple.campaigns[CAMPAIGN].model_copy(
        update={"shared_budgets": [sdk.SharedBudgetAssignment(budget_id=77)]}
    )
    apple.extra["shared_budgets_query_post"] = lambda **_: SimpleNamespace(
        result=[sdk.SharedBudget.model_validate({"id": 77, "name": "Example Budget",
                                                  "value": {"amount": "50", "currency": "USD"}})],
        error=None, pagination=SimpleNamespace(total_count=1))
    registrar = Registrar()
    tools_lookup.register(registrar, state)
    listed = registrar.tools["list_shared_budgets"]()
    assert listed.rows[0]["campaign_ids"] == [str(CAMPAIGN)], listed.rows
    assert listed.rows[0]["value_text"] == "50 USD"


def test_list_ads_pages() -> None:
    from apple_ads_mcp import tools_lookup

    _tools, apple, state = rig()
    registrar = Registrar()
    tools_lookup.register(registrar, state)
    listed = registrar.tools["list_ads"](ad_group_id=AD_GROUP)
    assert listed.returned == 1 and listed.total == 1 and listed.next_offset is None
    assert listed.rows[0]["name"] == "Example Ad"


def test_get_campaign_and_ad_group_are_expanded() -> None:
    _tools, apple, state = rig()
    apple.extra["campaigns_id_legacy_app_limited_status_reason_details_get"] = (
        lambda **_: SimpleNamespace(result=None, error=None)
    )
    tools = read_tools(state)
    campaign = tools["get_campaign"](campaign_id=CAMPAIGN)
    assert campaign.billing_event == "TAPS" and campaign.promoted_app_id == "1234567890"
    assert campaign.last_7d is not None and campaign.last_7d.spend == "70.00 USD"
    ad_group = tools["get_ad_group"](ad_group_id=AD_GROUP)
    assert ad_group.bid_strategy_type == "MANUAL_CPT" and ad_group.pricing_model == "CPT"
    assert ad_group.keyword_count == 1 and ad_group.ad_count == 1
    assert ad_group.targeting["deviceClass"]["include"] == ["IPHONE", "IPAD"]


def test_a_read_only_role_blocks_every_preview() -> None:
    tools, apple, _ = rig()
    apple.extra["get_user_acls"] = lambda **_: _acls(["API Account Read Only"])
    preview = tools["preview_campaign_status"](campaign_id=CAMPAIGN, status="PAUSED")
    assert preview.blocked
    assert any(c.startswith("FAIL role_can_write") for c in preview.guardrail_checks)


def test_reports_split_by_dimension_and_by_day() -> None:
    _tools, apple, state = rig()
    row = apple.report_row(CAMPAIGN, spend="14.00", taps=14, countryOrRegion="US")
    row["granularity"] = [
        {"date": "2026-11-01", "localSpend": {"amount": "6.00", "currency": "USD"}, "taps": 6,
         "impressions": 60},
        {"date": "2026-11-02", "localSpend": {"amount": "8.00", "currency": "USD"}, "taps": 8,
         "impressions": 80},
    ]
    apple.reports["apps_campaign_reports"] = [row]
    tools = read_tools(state)
    report = tools["campaign_report"](group_by=["countryOrRegion"], segment_by_date=True)
    assert [r.extra["date"] for r in report.rows] == ["2026-11-01", "2026-11-02"], report.rows
    assert all(r.extra["countryOrRegion"] == "US" for r in report.rows)
    request = [k["apps_reporting_request"] for m, k in apple.calls if m.endswith("_without_preload_content")][-1]
    assert request.group_by == ["countryOrRegion"]
    try:
        tools["campaign_report"](group_by=["browser"])
    except Exception as exc:
        assert "INVALID_REQUEST" in str(exc)
    else:
        raise AssertionError("an unknown dimension was sent to Apple")


def test_ad_report_narrows_to_one_ad() -> None:
    _tools, apple, state = rig()
    apple.reports["apps_ad_reports"] = [
        apple.report_row(5555, spend="14.00", taps=14, adGroupId=AD_GROUP),
        apple.report_row(5556, spend="9.00", taps=9, adGroupId=AD_GROUP),
    ]
    report = read_tools(state)["ad_report"](campaign_id=CAMPAIGN, ad_id=5556)
    assert [r.id for r in report.rows] == ["5556"], report.rows


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
