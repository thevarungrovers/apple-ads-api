#!/usr/bin/env python3
"""The daily cap: read from the ledger, shared by every process, local day.

Run it without touching Apple:
    ./venv/bin/python tests/test_daily_cap.py
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import sys
import tempfile
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import CAMPAIGN, KEYWORD, Registrar, limits, rig, run  # noqa: E402

from apple_ads_mcp import ledger, logdb, tools_write  # noqa: E402
from apple_ads_mcp.guardrails import DailyUsage, SessionCounters, local_midnight_utc  # noqa: E402
from apple_ads_mcp.previews import PreviewStore  # noqa: E402
from apple_ads_mcp.server import build_state  # noqa: E402


def _row(entry_id: str, ts: dt.datetime | str, outcome: str = "applied", delta: str = "5.00",
         extra: dict | None = None) -> None:
    ledger.write_intent(
        entry_id, tool="apply_keyword_bid", entity_type="keyword", entity_id=KEYWORD,
        entity_name="x", path="p", field="bid", before="1.00", after="2.00",
        campaign_id=CAMPAIGN, projected_daily_spend_delta=delta, extra=extra,
    )
    ledger.write_outcome(entry_id, outcome=outcome)
    stamp = ts if isinstance(ts, str) else ts.astimezone(dt.timezone.utc).isoformat(timespec="seconds")
    logdb.connect().execute("UPDATE changes SET ts=? WHERE entry_id=?", (stamp, entry_id))


def _raises(fn, needle: str) -> str:
    try:
        fn()
    except Exception as exc:
        assert needle in str(exc), f"expected {needle!r} in {exc!r}"
        return str(exc)
    raise AssertionError(f"expected a refusal mentioning {needle!r}")


def test_two_server_states_on_one_database_share_the_day() -> None:
    tools, _apple, _ = rig()
    first, second = build_state(), build_state()
    preview = tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.10")
    tools["apply_keyword_bid"](preview_token=preview.preview_token)
    assert DailyUsage(first.limits).usage()[0] == 1
    assert DailyUsage(second.limits).usage()[0] == 1, "the second process must see the first's apply"

    # A fresh process with a full SESSION budget is still refused by the DAY.
    lim = dataclasses.replace(second.limits, max_applies_per_day=1, allowed_currencies=frozenset({"USD"}))
    state = SimpleNamespace(limits=lim, counters=SessionCounters(lim), previews=PreviewStore())
    registrar = Registrar()
    tools_write.register(registrar, state)
    again = registrar.tools["preview_keyword_bid"](keyword_id=KEYWORD, new_bid="1.15")
    assert any("REFUSED" in w and "daily apply limit" in w for w in again.warnings), again.warnings
    _raises(lambda: registrar.tools["apply_keyword_bid"](preview_token=again.preview_token),
            "daily apply limit reached")


def test_yesterday_late_evening_does_not_count_today() -> None:
    rig()
    midnight = local_midnight_utc()
    _row("yesterday", midnight - dt.timedelta(minutes=1))
    _row("today", midnight + dt.timedelta(seconds=1))
    applies, delta = DailyUsage(limits()).usage()
    assert (applies, delta) == (1, Decimal("5.00")), (applies, delta)


def test_failed_refused_and_unfinished_rows_do_not_count() -> None:
    rig()
    now = dt.datetime.now(dt.timezone.utc)
    _row("ok", now)
    _row("failed", now, outcome="failed")
    _row("refused", now, outcome="refused")
    ledger.write_intent(
        "unfinished", tool="apply_keyword_bid", entity_type="keyword", entity_id=1,
        entity_name="x", path="p", field="bid", before="1", after="2", campaign_id=1,
        projected_daily_spend_delta="9.00",
    )

    assert DailyUsage(limits()).usage() == (1, Decimal("5.00"))


def test_a_batch_counts_once_and_a_creation_costs_its_weight() -> None:
    rig()

    now = dt.datetime.now(dt.timezone.utc)
    for index in range(3):
        _row(f"b{index}", now, delta="1.00", extra={"batch_id": "batch-1"})
    _row("create", now, delta="0", extra={"cost": 3})
    applies, delta = DailyUsage(limits()).usage()
    assert applies == 4, applies  # one batch + one creation costing 3
    assert delta == Decimal("3.00"), delta


def test_a_reduction_refunds_nothing() -> None:
    rig()

    now = dt.datetime.now(dt.timezone.utc)
    _row("up", now, delta="10.00")
    _row("down", now, delta="-30.00")
    assert DailyUsage(limits()).usage() == (2, Decimal("10.00"))


def test_legacy_rows_in_other_timestamp_spellings_are_judged_by_instant() -> None:
    rig()

    midnight = local_midnight_utc()
    today_z = (midnight + dt.timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    yesterday_offset = (midnight - dt.timedelta(minutes=5)).astimezone(
        dt.timezone(dt.timedelta(hours=-4))
    ).isoformat()
    tmp = tempfile.TemporaryDirectory()
    path = pathlib.Path(tmp.name) / "changes.jsonl"
    lines = []
    for entry_id, ts in (("legacy-today", today_z), ("legacy-yesterday", yesterday_offset)):
        lines.append({"record": "intent", "entry_id": entry_id, "ts": ts, "tool": "apply_keyword_bid",
                      "entity_type": "keyword", "entity_id": 1, "field": "bid", "before": "1",
                      "after": "2", "projected_daily_spend_delta": "4.00"})
        lines.append({"record": "outcome", "entry_id": entry_id, "ts": ts, "outcome": "applied",
                      "observed_after": "2"})
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n")
    assert ledger.import_legacy_jsonl(path)["inserted"] == 2
    assert DailyUsage(limits()).usage() == (1, Decimal("4.00"))
    tmp.cleanup()


def test_the_daily_total_blocks_a_preview_and_names_both_figures() -> None:
    tools, _apple, _ = rig(max_projected_delta_per_day_total=Decimal("20.00"))
    _row("earlier", dt.datetime.now(dt.timezone.utc), delta="19.50")
    preview = tools["preview_campaign_daily_budget"](campaign_id=CAMPAIGN, new_daily_budget="110.00")
    assert preview.blocked
    line = next(c for c in preview.guardrail_checks if "projected_daily_total" in c)
    assert line.startswith("FAIL") and "10.00" in line and "0.50" in line, line


def test_get_guardrails_reports_the_day() -> None:
    from apple_ads_mcp import tools_read

    rig()
    _row("one", dt.datetime.now(dt.timezone.utc))
    state = build_state()
    registrar = Registrar()
    tools_read.register(registrar, state)
    report = registrar.tools["get_guardrails"]()
    assert report.daily["applies_today"] == "1", report.daily
    assert "max_projected_delta_per_day_total" in report.limits


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
