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


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
