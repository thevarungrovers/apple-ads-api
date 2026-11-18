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

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import AD_GROUP, Registrar, rig, run  # noqa: E402

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


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
