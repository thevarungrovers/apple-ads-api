#!/usr/bin/env python3
"""The break-glass CLI: humans only, kill switch honoured, every write ledgered.

Nothing here reaches Apple: the request function and the token are replaced.

Run it without touching Apple:
    ./venv/bin/python tests/test_cli.py
"""

from __future__ import annotations

import argparse
import io
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import no_live_client, run, temp_db  # noqa: E402

import apple_ads_cli as cli  # noqa: E402
import install  # noqa: E402

from apple_ads_mcp import ledger  # noqa: E402
from apple_ads_mcp.guardrails import DailyUsage  # noqa: E402
from kindkit import limits  # noqa: E402


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _args(**over) -> argparse.Namespace:
    base = dict(path="campaigns/1234567890", method="PUT", data='{"name": "Renamed"}',
                apply=True, confirm_live=True)
    base.update(over)
    return argparse.Namespace(**base)


def _wire(sent: list, *, tty: bool, typed: str, status: int = 200) -> None:
    no_live_client()
    temp_db()
    cli.writes_disabled = lambda: None
    cli.resolve_ad_account_id = lambda: "1234567890"
    cli.get_api = lambda: type("A", (), {"api_client": type("C", (), {
        "_access_token_provider": type("P", (), {"get_token": lambda self: "t"})()})()})()
    cli.context_header = lambda *_a: "adAccountId=1234567890;"
    stream = _Tty() if tty else io.StringIO()
    cli._input_stream = lambda: stream
    cli.input = lambda _prompt: typed

    def fake_request(method, path, body, token):
        sent.append((method, path, body))
        return status, json.dumps({"result": {"id": 1}})

    cli.request = fake_request


def test_apply_without_a_terminal_sends_nothing() -> None:
    sent: list = []
    _wire(sent, tty=False, typed="campaigns/1234567890")
    assert cli._run(_args()) == 1
    assert sent == []


def test_apply_with_a_wrong_confirmation_sends_nothing() -> None:
    sent: list = []
    _wire(sent, tty=True, typed="campaigns/1")
    assert cli._run(_args()) == 1
    assert sent == []


def test_the_kill_switch_stops_the_cli() -> None:
    sent: list = []
    _wire(sent, tty=True, typed="campaigns/1234567890")
    cli.writes_disabled = lambda: "frozen"
    assert "writes are disabled" in cli.confirm_apply("campaigns/1234567890", stdin=_Tty(),
                                                      ask=lambda _p: "campaigns/1234567890")
    assert cli._run(_args()) == 1 and sent == []


def test_a_confirmed_write_is_sent_and_ledgered_and_counts_today() -> None:
    sent: list = []
    _wire(sent, tty=True, typed="campaigns/1234567890")
    assert cli._run(_args()) == 0
    assert sent and sent[0][0] == "PUT"
    rows = ledger.entries()
    assert len(rows) == 1 and rows[0]["tool"] == "cli" and rows[0]["outcome"] == "applied", rows
    assert rows[0]["entity_id"] == "1234567890"
    assert DailyUsage(limits()).usage()[0] == 1


def test_a_rejected_write_is_ledgered_as_failed() -> None:
    sent: list = []
    _wire(sent, tty=True, typed="campaigns/1234567890", status=400)
    assert cli._run(_args()) == 1
    assert ledger.entries()[0]["outcome"] == "failed"


def test_a_cli_write_that_times_out_is_ledgered_as_unknown_and_counts_today() -> None:
    sent: list = []
    _wire(sent, tty=True, typed="campaigns/1234567890")

    def times_out(method, path, body, token):
        sent.append((method, path, body))
        raise TimeoutError("read timed out")

    cli.request = times_out
    try:
        cli._run(_args())
    except TimeoutError:
        pass
    else:
        raise AssertionError("expected the timeout to propagate")
    assert ledger.entries()[0]["outcome"] == "unknown"
    assert DailyUsage(limits()).usage()[0] == 1


def test_a_read_needs_no_confirmation_and_writes_no_ledger_row() -> None:
    sent: list = []
    _wire(sent, tty=False, typed="")
    assert cli._run(_args(method="POST", path="campaigns/query", data="{}", apply=False)) == 0
    assert sent and ledger.entries() == []


BYPASS_PATHS = (
    "campaigns?x=/query",
    "campaigns/?x=/query",
    "keywords/bulk?_=/query",
    "campaigns/1234567890/adgroups/1/targetingkeywords/bulk?_=/query",
    "campaigns#/query",
    "campaigns/1234567890#/query",
    "campaigns/query?x=1",
    "campaigns/query#frag",
    "campaigns/1234567890/../query",
    "campaigns/%71uery",
    "campaigns%2Fquery",
    "query",
    "/query",
    "",
)


def test_a_post_hiding_query_in_its_query_string_or_fragment_is_a_mutation() -> None:
    for path in BYPASS_PATHS:
        assert cli.is_mutation("POST", path), path
        assert cli.is_mutation("post", path), path


def test_a_real_query_post_is_still_a_read() -> None:
    for path in ("campaigns/query", "/campaigns/query/", "reports/apps/campaigns/query",
                 "campaigns/1234567890/adgroups/query", "change-history/query",
                 "Campaigns/Query"):
        assert not cli.is_mutation("POST", path), path


def test_an_unknown_method_fails_closed() -> None:
    assert not cli.is_mutation("GET", "campaigns/1234567890?x=1")
    for method in ("PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "FOO", ""):
        assert cli.is_mutation(method, "campaigns/query"), method


def test_a_bypass_post_without_apply_sends_nothing() -> None:
    for path in BYPASS_PATHS[:5]:
        sent: list = []
        _wire(sent, tty=False, typed="")
        assert cli._run(_args(method="POST", path=path, data="{}", apply=False)) == 0
        assert sent == [], path


def test_a_bypass_post_with_apply_but_no_terminal_sends_nothing() -> None:
    sent: list = []
    _wire(sent, tty=False, typed="")
    assert cli._run(_args(method="POST", path="campaigns?x=/query", data="{}")) == 1
    assert sent == [] and ledger.entries() == []


def test_the_installer_denies_the_cli_apply_to_the_agent() -> None:
    assert "Bash(*apple_ads_cli.py*--apply*)" in install.CLI_DENY_RULES


def test_the_server_no_longer_tells_the_agent_to_run_the_cli() -> None:
    from apple_ads_mcp.server import INSTRUCTIONS

    assert "Use the break-glass CLI" not in INSTRUCTIONS
    assert "never run `apple_ads_cli.py ... --apply` yourself" in INSTRUCTIONS


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
