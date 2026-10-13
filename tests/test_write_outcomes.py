#!/usr/bin/env python3
"""A revert's outcome must follow the READ-BACK, not the HTTP status.

The live account can demonstrate the happy path -- pause a keyword, revert it,
watch it come back -- and it cannot demonstrate the other one, because you
cannot ask Apple to accept a write and ignore it on demand. That other path is
the one with the bad consequence: a revert Apple silently discarded, reported as
a successful undo, tells the operator the account is back where it was and stops
them looking.

So it is stubbed here. `client.call` is replaced with a fake Apple that echoes
whatever the scenario says, and the assertion is on the LEDGER ROW, which is what
`revert_change` and `reconcile_ledger` both read later.

This test fails against the code before this branch: `_revert_result` recorded
`APPLIED` unconditionally, so `test_a_discarded_revert_is_recorded_as_failed`
would see `applied` and `reverted_entry_ids()` would contain the target.

Run it without touching Apple:
    ./venv/bin/python tests/test_write_outcomes.py
"""

from __future__ import annotations

import pathlib
import sys
from decimal import Decimal

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests"))

from apple_ads_mcp import client, ledger, logdb, tools_write  # noqa: E402
from apple_ads_mcp.server import build_state  # noqa: E402
from test_logdb import use_temp_db  # noqa: E402

TARGET = "target-entry"
KEYWORD_ID = 123


class _Keyword:
    """Just enough of an Apple keyword for the revert path to read."""

    def __init__(self, status: str) -> None:
        self.id = KEYWORD_ID
        self.status = status
        self.campaign_id = 999
        self.ad_group_id = 888
        self.text = "organic kale"


class _Response:
    def __init__(self, result) -> None:
        self.result = result
        self.error = None


class FakeApple:
    """Echoes a scripted status back. Records what it was asked to do.

    `put_echoes` is the whole point: set it to the value the revert ASKED for and
    the write landed; set it to the value already there and Apple accepted the
    request and ignored it, which is what an automated bid strategy does and what
    no amount of status-code checking would reveal.
    """

    def __init__(self, current: str, put_echoes: str) -> None:
        self.current = current
        self.put_echoes = put_echoes
        self.calls: list[str] = []

    def __call__(self, method_name: str, /, **kwargs):
        self.calls.append(method_name)
        if method_name == "keywords_id_get":
            return _Response(_Keyword(self.current))
        if method_name == "keywords_id_put":
            return _Response(_Keyword(self.put_echoes))
        raise AssertionError(f"unexpected API call: {method_name}")


class Registrar:
    """Captures the tool functions instead of publishing them over MCP."""

    def __init__(self) -> None:
        self.tools: dict = {}

    def tool(self, **_kwargs):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn

        return decorate


def build_revert(fake: FakeApple):
    """A `revert_change` wired to `fake`, with a ledger entry already applied."""
    use_temp_db()
    logdb.insert_change(
        TARGET,
        tool="apply_keyword_status",
        entity_type="keyword",
        entity_id=KEYWORD_ID,
        entity_name="organic kale",
        path="Campaign 'C' > AdGroup 'A' > Keyword 'organic kale'",
        field="status",
        before="ENABLED",
        after="PAUSED",
        campaign_id=999,
        projected_daily_spend_delta="0",
    )
    logdb.complete_change(TARGET, outcome=logdb.APPLIED, observed_after="PAUSED")

    client.call = fake
    tools_write.call = fake
    # The kill switch is a real file on disk; a developer who happens to have one
    # set would otherwise see this test fail for an unrelated reason.
    tools_write.writes_disabled = lambda: None

    registrar = Registrar()
    tools_write.register(registrar, build_state())
    return registrar.tools["revert_change"]


def test_a_revert_that_lands_is_recorded_as_applied() -> None:
    """Apple echoes ENABLED, which is what was asked for."""
    fake = FakeApple(current="PAUSED", put_echoes="ENABLED")
    revert = build_revert(fake)

    result = revert(entry_id=TARGET)

    assert result.applied is True, result.failures
    assert result.observed_after == "ENABLED"
    assert "keywords_id_put" in fake.calls
    row = ledger.find_entry(result.entry_id)
    assert row["outcome"] == ledger.APPLIED, row
    assert TARGET in ledger.reverted_entry_ids(), (
        "a revert that landed must mark its target as reverted"
    )


def test_a_discarded_revert_is_recorded_as_failed() -> None:
    """Apple answers 200 and echoes PAUSED -- the value that was already there.

    The regression this pins: before this branch the outcome was APPLIED
    regardless, so the operator was told the undo worked when nothing moved.
    """
    fake = FakeApple(current="PAUSED", put_echoes="PAUSED")
    revert = build_revert(fake)

    result = revert(entry_id=TARGET)

    assert result.applied is False, "a revert Apple ignored is not a successful undo"
    assert result.observed_after == "PAUSED"
    assert result.failures, "the caller must be told, not just the ledger"
    assert "NOT REVERTED" in result.note

    row = ledger.find_entry(result.entry_id)
    assert row["outcome"] == ledger.FAILED, row
    assert "PAUSED" in (row["outcome_detail"] or ""), row["outcome_detail"]


def test_a_discarded_revert_leaves_its_target_revertable() -> None:
    """The reason recording it honestly matters, rather than just being tidy.

    `reverted_entry_ids()` counts only APPLIED reverts, so a failed one keeps the
    original entry eligible and the retry available once the cause is fixed. The
    old behaviour burned the only attempt.
    """
    fake = FakeApple(current="PAUSED", put_echoes="PAUSED")
    revert = build_revert(fake)
    revert(entry_id=TARGET)

    assert TARGET not in ledger.reverted_entry_ids(), (
        "a revert that did not land must not consume the target's one undo"
    )
    # And the retry really is available: the same call runs again rather than
    # being refused with "entry has already been reverted".
    retry = revert(entry_id=TARGET)
    assert retry.applied is False
    assert ledger.find_entry(retry.entry_id)["outcome"] == ledger.FAILED


def test_the_session_applies_cap_bounds_revert_change() -> None:
    """A revert is a real write, so it counts against max_applies_per_session.

    Without this, a loop could revert every applied entry in the ledger's whole
    window -- including changes made in earlier sessions, which this session's
    counter has never seen -- with no bound at all.
    """
    fake = FakeApple(current="PAUSED", put_echoes="ENABLED")
    revert = build_revert(fake)

    state = build_state()
    registrar = Registrar()
    tools_write.register(registrar, state)
    revert = registrar.tools["revert_change"]

    state.counters.applies = state.counters.limits.max_applies_per_session
    try:
        revert(entry_id=TARGET)
    except Exception as exc:  # ToolError
        assert "session limit reached" in str(exc), exc
    else:
        raise AssertionError("revert_change ignored the session applies cap")


def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
    print(f"\n{len(tests)} tests.")
    print("All checks passed." if not failures else f"{failures} failure(s).")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
