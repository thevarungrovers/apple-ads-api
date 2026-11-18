#!/usr/bin/env python3
"""The naming convention: `<OWNER>-<agent>-<base>`, from `.env` only.

Placeholders only (`AB` / `max`, and `CD` / `bot` for a second identity). The
real values live in an operator's `.env` and never in this repository.

    ./venv/bin/python tests/test_naming.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from kindkit import run  # noqa: E402

from apple_ads_mcp import naming  # noqa: E402
from apple_ads_mcp.naming import Identity, NameRefused, classify, parse, rename, stamp  # noqa: E402

BOTH = Identity.from_values("AB", "max", ".env")
OWNER_ONLY = Identity.from_values("AB", "", ".env")
AGENT_ONLY = Identity.from_values("", "max", ".env")
NEITHER = Identity.from_values("", "", "")
COMBOS = {
    "both": (BOTH, "AB-max-spring_promo"),
    "owner": (OWNER_ONLY, "AB-spring_promo"),
    "agent": (AGENT_ONLY, "max-spring_promo"),
    "neither": (NEITHER, "spring_promo"),
}


def _refused(fn, needle: str) -> str:
    try:
        fn()
    except NameRefused as exc:
        assert needle in str(exc), f"expected {needle!r} in {exc}"
        return str(exc)
    raise AssertionError(f"expected NameRefused mentioning {needle!r}")


def test_the_four_identity_combinations_stamp_a_create() -> None:
    for label, (identity, expected) in COMBOS.items():
        result = stamp(identity, "spring_promo", entity_type="campaign")
        assert result.final == expected, (label, result.final)
        assert result.base == "spring_promo" and not result.failures, (label, result)
    assert stamp(NEITHER, "  spring_promo ", entity_type="ad").block()["rule"] == naming.RULES["none"]
    assert stamp(NEITHER, "spring_promo", entity_type="ad").warning() is None
    assert "AB-max-spring_promo" in stamp(BOTH, "spring_promo", entity_type="ad").warning()


def test_our_prefix_is_never_added_twice() -> None:
    for label, (identity, expected) in COMBOS.items():
        if not identity.configured:
            continue
        again = stamp(identity, expected, entity_type="campaign")
        assert again.final == expected and again.rule == "already_ours", (label, again)


def test_a_doubled_prefix_is_refused_not_kept() -> None:
    _refused(lambda: stamp(BOTH, "AB-max-AB-max-x", entity_type="campaign"), "twice")
    _refused(lambda: stamp(OWNER_ONLY, "AB-AB-x", entity_type="campaign"), "twice")
    _refused(lambda: stamp(AGENT_ONLY, "max-max-x", entity_type="campaign"), "twice")
    # Our prefix, then someone else's: the base still parses as a prefix.
    _refused(lambda: stamp(BOTH, "AB-max-CD-bot-x", entity_type="campaign"), "CD-bot-")
    _refused(lambda: rename(BOTH, "AB-max-a", "AB-max-AB-max-b", entity_type="ad_group",
                            ledger_owned=False), "twice")


def test_another_agents_prefix_is_refused() -> None:
    _refused(lambda: stamp(BOTH, "CD-bot-launch", entity_type="campaign"), "CD-bot-")
    _refused(lambda: stamp(OWNER_ONLY, "CD-bot-launch", entity_type="campaign"), "CD-bot-")
    # Only the initials are set and the base would read as agent max's.
    _refused(lambda: stamp(OWNER_ONLY, "AB-max-launch", entity_type="campaign"), "impersonat")
    _refused(lambda: stamp(OWNER_ONLY, "max-launch", entity_type="campaign"), "would read as")
    # With nothing configured a plain name is used exactly as given...
    assert stamp(NEITHER, "launch", entity_type="campaign").final == "launch"


def test_a_name_attributed_to_someone_else_is_refused_even_with_no_identity() -> None:
    """Accepting `CD-bot-x` verbatim would forge agent bot's attribution."""
    for identity in (NEITHER, AGENT_ONLY, NEITHER.cli(), AGENT_ONLY.cli()):
        message = _refused(lambda identity=identity: stamp(identity, "CD-bot-launch",
                                                           entity_type="campaign"), "CD-bot-")
        assert "impersonat" in message, message
    _refused(lambda: stamp(OWNER_ONLY, "AB-cli-launch", entity_type="campaign"), "impersonat")
    _refused(lambda: stamp(NEITHER, "AB-max-launch", entity_type="ad"), "impersonat")
    _refused(lambda: rename(NEITHER, "launch", "CD-bot-x", entity_type="campaign",
                            ledger_owned=True), "impersonat")


def test_empty_and_control_characters_are_refused() -> None:
    for identity, _ in COMBOS.values():
        _refused(lambda: stamp(identity, "   ", entity_type="campaign"), "empty")
        _refused(lambda: stamp(identity, "a\tb", entity_type="campaign"), "control character")
    _refused(lambda: stamp(BOTH, "AB-max-", entity_type="campaign"), "only the prefix")


def test_invisible_and_format_characters_are_refused() -> None:
    for char in ("\u200b", "\u200e", "\u2028", "\u2029", "\ufeff", "\u0085", "\u202e"):
        for identity, _ in COMBOS.values():
            _refused(lambda identity=identity, char=char: stamp(
                identity, f"spring{char}promo", entity_type="campaign"), "invisible")
    assert stamp(BOTH, "printemps \u00e9t\u00e9 \u2014 promo", entity_type="ad").final.endswith("promo")


def test_the_length_cap_counts_the_prefix_and_never_truncates() -> None:
    limit = naming.NAME_LIMITS["campaign"]
    assert limit == 200
    fits = "x" * (limit - len("AB-max-"))
    assert stamp(BOTH, fits, entity_type="campaign").failures == []
    over = stamp(BOTH, fits + "y", entity_type="campaign")
    assert over.final == "AB-max-" + fits + "y", "never truncated"
    [(check, detail)] = over.failures
    assert check == "name_length" and f"at most {limit - 7}" in detail, detail
    assert stamp(NEITHER, "x" * limit, entity_type="ad").failures == []
    assert stamp(NEITHER, "x" * (limit + 1), entity_type="ad").failures[0][0] == "name_length"


def test_a_malformed_value_is_refused_naming_its_env_key() -> None:
    bad_owner = Identity.from_values("a1", "max")
    [problem] = bad_owner.problems
    assert naming.OWNER_KEY in problem, problem
    result = stamp(bad_owner, "x", entity_type="campaign")
    assert ("agent_identity", problem) in result.failures
    for agent in ("Max", "m", "has-dash", "cli", "manual", "x" * 25):
        identity = Identity.from_values("AB", agent)
        assert identity.problems and naming.AGENT_KEY in identity.problems[0], agent
    for owner in ("A", "ABCDE", "ab", "A1"):
        assert Identity.from_values(owner, "").problems, owner
    assert not Identity.from_values("ABCD", "a_b2").problems


def test_the_parser() -> None:
    assert parse("AB-max-spring") == naming.Parsed("AB", "max", "spring", "agent")
    assert parse("AB-cli-spring").origin == "cli"
    assert parse("AB-max-") == naming.Parsed("AB", "max", "", "agent")
    for name in ("spring", "AB-spring", "max-spring", "ab-max-x", "ABCDE-max-x", "AB-m-x", "", None):
        assert parse(name) is None, name
    # One-part prefixes are recognised only as this checkout's own.
    assert classify("AB-spring", OWNER_ONLY).origin == "agent"
    assert classify("AB-spring", AGENT_ONLY).origin == "manual"
    assert classify("max-spring", AGENT_ONLY).base == "spring"
    assert classify("CD-bot-x", BOTH) == naming.Parsed("CD", "bot", "x", "agent")
    assert classify("launch", BOTH).origin == "manual"


def test_renames_only_the_agents_own_entities() -> None:
    for label, (identity, ours) in COMBOS.items():
        # Its own by prefix (when there is one) or by the ledger.
        if identity.configured:
            renamed = rename(identity, ours, "autumn", entity_type="campaign", ledger_owned=False)
            assert renamed.final == ours.replace("spring_promo", "autumn"), (label, renamed)
            assert renamed.rule == "kept_prefix"
        by_ledger = rename(identity, "launch", "autumn", entity_type="campaign", ledger_owned=True)
        assert by_ledger.final == identity.prefix + "autumn", (label, by_ledger)
        # A manual entity, never created here: refused, with no override.
        _refused(lambda: rename(identity, "launch", "autumn", entity_type="campaign",
                                ledger_owned=False), "not this agent's")
    _refused(lambda: rename(BOTH, "CD-bot-launch", "x", entity_type="ad_group", ledger_owned=False),
             "CD-bot-")
    restored = rename(BOTH, "launch", "launch2", entity_type="campaign", ledger_owned=True)
    assert restored.rule == "restored_prefix" and restored.final == "AB-max-launch2"


def test_the_ledger_cannot_make_another_full_identitys_name_ours() -> None:
    """A creation row in this ledger does not make `CD-bob-x` ours: its name
    says it is CD-bob's, and renaming it would re-stamp it as ours."""
    for identity in (BOTH, OWNER_ONLY, AGENT_ONLY, NEITHER):
        for current in ("CD-bob-x", "AB-bot-x", "AB-cli-x"):
            if (identity.owner, identity.agent) == tuple(current.split("-")[:2]):
                continue
            _refused(lambda identity=identity, current=current: rename(
                identity, current, "y", entity_type="campaign", ledger_owned=True),
                "not this agent's")
    # Our own full identity, or a name that parses as no identity, still renames.
    assert rename(BOTH, "AB-max-x", "y", entity_type="campaign", ledger_owned=True).final == "AB-max-y"
    assert rename(BOTH, "x", "y", entity_type="campaign", ledger_owned=True).final == "AB-max-y"


def test_created_by_us_compares_the_identity_on_the_creation_row() -> None:
    from kindkit import temp_db

    from apple_ads_mcp import ledger

    temp_db()

    def created(entity_id: str, identity: dict | None) -> None:
        entry_id = ledger.new_entry_id()
        ledger.write_intent(
            entry_id, tool="apply_create_campaign", entity_type="campaign", entity_id=entity_id,
            entity_name="x", path="-", field="created", before=None, after="PAUSED",
            campaign_id=None, projected_daily_spend_delta="0",
            extra={"detail": {"identity": identity}} if identity is not None else None,
        )
        ledger.write_outcome(entry_id, outcome=ledger.APPLIED)

    created("1", {"owner": "CD", "agent": "bob", "source": ".env"})
    created("2", {"owner": "AB", "agent": "max", "source": ".env"})
    created("3", None)  # a row from before identities were recorded
    created("4", {"owner": "AB", "agent": None, "source": ".env"})
    assert not ledger.created_by_us("campaign", "1", BOTH)
    assert ledger.created_by_us("campaign", "2", BOTH)
    assert ledger.created_by_us("campaign", "3", BOTH)
    assert not ledger.created_by_us("campaign", "4", BOTH)
    assert ledger.created_by_us("campaign", "4", OWNER_ONLY)
    assert not ledger.created_by_us("campaign", "2", OWNER_ONLY)
    assert ledger.created_by_us("campaign", "1")  # no identity to compare: any creation


def test_ownership_needs_the_whole_prefix_not_just_its_start() -> None:
    """`AB-` is a prefix of `AB-bot-x` and `AB-cli-x`, but neither is ours: a
    name that parses as a full two-part prefix is ours only when both parts are."""
    for name in ("AB-bot-x", "AB-cli-x"):
        assert not OWNER_ONLY.owns(name), name
        _refused(lambda name=name: rename(OWNER_ONLY, name, "newname", entity_type="campaign",
                                          ledger_owned=False), "not this agent's")
        keep = naming.name_filter(OWNER_ONLY, mine=True)
        assert not keep(name), name
    assert OWNER_ONLY.owns("AB-x") and OWNER_ONLY.owns("AB-Spring-sale")
    assert not OWNER_ONLY.owns("AB-spring-sale"), "reads as agent spring's"
    # Agent only: `max-` never parses (no initials), so the start is enough.
    assert AGENT_ONLY.owns("max-x") and not AGENT_ONLY.owns("AB-max-x")
    # Both set: only our own full prefix.
    assert BOTH.owns("AB-max-x") and not BOTH.owns("AB-maxi-x") and not BOTH.owns("AB-bot-x")
    # The CLI identity (AB, cli) owns its own names and nobody else's.
    assert OWNER_ONLY.cli().owns("AB-cli-x") and BOTH.cli().owns("AB-cli-x")
    assert not BOTH.cli().owns("AB-max-x") and not OWNER_ONLY.cli().owns("AB-x")
    assert not NEITHER.owns("AB-x")


def test_the_cli_identity() -> None:
    assert BOTH.cli().prefix == "AB-cli-"
    assert OWNER_ONLY.cli().prefix == "AB-cli-"
    assert AGENT_ONLY.cli().prefix == "", "no owner: nothing to mark"
    assert NEITHER.cli().prefix == ""
    assert stamp(BOTH.cli(), "launch", entity_type="campaign").final == "AB-cli-launch"
    assert stamp(BOTH.cli(), "AB-cli-launch", entity_type="campaign").final == "AB-cli-launch"
    _refused(lambda: stamp(BOTH.cli(), "AB-max-launch", entity_type="campaign"), "AB-max-")


def test_identity_comes_from_env_files_and_a_nonempty_dotenv_value_wins() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        config_env, dotenv = root / "config.env", root / ".env"
        config_env.write_text("APPLE_ADS_OWNER_INITIALS=CD\nAPPLE_ADS_AGENT_NAME=bot\n")
        dotenv.write_text("APPLE_ADS_OWNER_INITIALS=AB\nAPPLE_ADS_AGENT_NAME=\n")
        identity = Identity.load([config_env, dotenv])
        # .env wins where it has a value; an EMPTY .env value does not erase
        # config.env's.
        assert (identity.owner, identity.agent) == ("AB", "bot"), identity
        assert identity.source == "config.env + .env"
        dotenv.write_text("APPLE_ADS_OWNER_INITIALS=\nAPPLE_ADS_AGENT_NAME=\n")
        identity = Identity.load([config_env, dotenv])
        assert (identity.owner, identity.agent) == ("CD", "bot"), identity
        assert identity.source == "config.env", identity.source
        assert Identity.load([root / "missing.env"]) == Identity()


def test_the_process_env_is_ignored_and_a_mismatch_is_reported() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        dotenv = pathlib.Path(tmp) / ".env"
        dotenv.write_text("APPLE_ADS_OWNER_INITIALS=AB\nAPPLE_ADS_AGENT_NAME=max\n")
        saved = naming.ENV_FILES
        naming.ENV_FILES = [dotenv]
        try:
            identity = Identity.load()
            assert identity.prefix == "AB-max-"
            env = {naming.OWNER_KEY: "CD", naming.AGENT_KEY: "max"}
            [warning] = naming.process_env_warnings(identity, env)
            assert naming.OWNER_KEY in warning and "ignored" in warning
            assert "CD" not in warning, "the warning names the key, not the value"
            assert naming.process_env_warnings(identity, {}) == []
        finally:
            naming.ENV_FILES = saved


def test_the_server_state_reads_the_identity_and_warns_on_a_process_env_mismatch() -> None:
    import contextlib
    import io
    import os

    from apple_ads_mcp.server import build_state

    stderr = io.StringIO()
    saved = os.environ.get(naming.AGENT_KEY)
    os.environ[naming.AGENT_KEY] = "bot"
    try:
        with contextlib.redirect_stderr(stderr):
            state = build_state()
    finally:
        if saved is None:
            os.environ.pop(naming.AGENT_KEY, None)
        else:
            os.environ[naming.AGENT_KEY] = saved
    assert state.identity.prefix == "AB-max-", "the process env must not win"
    assert naming.AGENT_KEY in stderr.getvalue() and "ignored" in stderr.getvalue()


if __name__ == "__main__":
    raise SystemExit(run(dict(globals())))
