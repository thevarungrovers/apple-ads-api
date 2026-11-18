"""Names the agent gives what it creates: `<OWNER>-<agent>-<base>`.

A month after an agent created a campaign, nobody can tell it from one a person
made in the Apple Ads UI. So every name this server creates (and every name the
break-glass CLI creates) starts with who made it:

    AB-max-spring_promo        both parts set     (placeholders, illustration only)
    AB-spring_promo            only the initials
    max-spring_promo           only the agent name
    spring_promo               neither: the name is used exactly as given

THE IDENTITY COMES FROM THIS REPO'S `.env`, AND NOTHING ELSE. Two keys,
`APPLE_ADS_OWNER_INITIALS` and `APPLE_ADS_AGENT_NAME`, read with the same
`dotenv_values` loader `generate_client_secret.load_config` uses (and from a
`config.env` beside it, if one exists; a non-empty `.env` value wins).
Neither is in REQUIRED_KEYS: an empty or missing value is simply skipped. The process environment is
IGNORED -- no tool argument and no env block can change or skip the prefix --
and a value there that disagrees with `.env` is reported at startup so the
mismatch is visible. One identity per checkout: a second agent needs its own
checkout with its own `.env`.

A value that is SET but malformed is refused at preview, naming the `.env` key:
a bad prefix would make the name ambiguous to parse.

    owner initials  ^[A-Z]{2,4}$
    agent name      ^[a-z][a-z0-9_]{1,23}$   (no `-`; `cli` and `manual` reserved)

Neither part can contain `-`, so a name with both parts parses unambiguously.
A name with only one part cannot be told apart from a hand-made one by shape
alone (`spring-sale` looks like agent `spring`), so it is recognised only as
OURS -- by this checkout's own prefix -- or through the ledger.

LENGTH IS REFUSED, NEVER TRUNCATED. Apple has no validate-only mode, so a name
it rejects is found out only after a human approved it. The SDK models give no
maximum beyond `min_length=1`; 200 is a conservative cap (the older Campaign
Management API's documented limit), applied to the FULL name, prefix included.
"""

from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

from apple_ads_mcp.config import REPO_ROOT

OWNER_KEY = "APPLE_ADS_OWNER_INITIALS"
AGENT_KEY = "APPLE_ADS_AGENT_NAME"

OWNER_RE = re.compile(r"[A-Z]{2,4}", re.ASCII)
AGENT_RE = re.compile(r"[a-z][a-z0-9_]{1,23}", re.ASCII)
#: `cli` marks a human's creation through apple_ads_cli.py; `manual` is what a
#: report calls a name with no prefix. No agent may take either.
RESERVED_AGENTS = frozenset({"cli", "manual"})
CLI_AGENT = "cli"

#: A name with BOTH parts: the only shape that parses without knowing whose it is.
FULL_PREFIX = re.compile(r"([A-Z]{2,4})-([a-z][a-z0-9_]{1,23})-(.*)", re.ASCII | re.S)

#: Conservative: see the module docstring. Counts the whole name, prefix included.
MAX_NAME_LENGTH = 200
NAME_LIMITS: dict[str, int] = {
    "campaign": MAX_NAME_LENGTH,
    "ad_group": MAX_NAME_LENGTH,
    "ad": MAX_NAME_LENGTH,
    "creative": MAX_NAME_LENGTH,
}

#: Read in order; a later file wins. Module-level so tests point it at a
#: temporary `.env` -- the real one holds real values and is never read by tests.
ENV_FILES: list[Path] = [REPO_ROOT / "config.env", REPO_ROOT / ".env"]

#: Control (Cc), format (Cf: zero-width space, bidi marks, BOM...), line and
#: paragraph separators (Zl, Zp). None shows up on screen, so a name carrying
#: one is not the name a human read and approved.
_INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


#: Blank fillers that render as nothing but are letters or marks, so the
#: categories above miss them: the Hangul fillers and the combining grapheme
#: joiner. Refused anywhere in a name.
INVISIBLE_FILLERS = frozenset("\u115f\u1160\u3164\uffa0\u034f")

#: Characters that read as `-` but are not it, beyond dash punctuation (Pd):
#: the minus signs, the hyphen bullet, the modifier minus and the heavy minus.
_DASH_LOOKALIKES = frozenset("\u2212\u2043\u02d7\u2796\ufe58\ufe63\uff0d")

#: Cyrillic and Greek letters that render like a Latin one. Small and explicit:
#: it only has to catch what would make a name READ as a prefix.
_CONFUSABLES = str.maketrans({
    # Cyrillic lowercase
    "\u0430": "a", "\u0435": "e", "\u043e": "o", "\u0440": "p", "\u0441": "c",
    "\u0445": "x", "\u0443": "y", "\u0456": "i", "\u0458": "j", "\u0455": "s",
    # Cyrillic uppercase
    "\u0410": "A", "\u0412": "B", "\u0415": "E", "\u041a": "K", "\u041c": "M",
    "\u041d": "H", "\u041e": "O", "\u0420": "P", "\u0421": "C", "\u0422": "T",
    "\u0425": "X", "\u0414": "D",
    # Greek uppercase
    "\u0391": "A", "\u0392": "B", "\u0395": "E", "\u0396": "Z", "\u0397": "H",
    "\u0399": "I", "\u039a": "K", "\u039c": "M", "\u039d": "N", "\u039f": "O",
    "\u03a1": "P", "\u03a4": "T", "\u03a7": "X", "\u03a5": "Y",
})

#: What a full two-part prefix looks like once folded to its skeleton.
_SKELETON_PREFIX = re.compile(r"[A-Z]{2,4}-[a-z][a-z0-9_]{1,23}-", re.ASCII)


def invisible_character(text: str) -> str | None:
    """The first control, format, separator or filler character in ``text``, if any."""
    return next(
        (
            c
            for c in text
            if unicodedata.category(c) in _INVISIBLE_CATEGORIES or c in INVISIBLE_FILLERS
        ),
        None,
    )


def _fold(c: str) -> str:
    """One character's skeleton: NFKC, every dash look-alike to `-`, confusables to Latin."""
    return "".join(
        "-" if x in _DASH_LOOKALIKES or unicodedata.category(x) == "Pd" else x.translate(_CONFUSABLES)
        for x in unicodedata.normalize("NFKC", c)
    )


def lookalike_in_prefix(name: str | None, own_prefix: str = "") -> str | None:
    """The first look-alike character that makes ``name`` READ as a prefix, if any.

    Every ownership and impersonation check here is ASCII (FULL_PREFIX,
    startswith), so `C\u0421-bot-x` with a Cyrillic letter, or `CD\u2010bot\u2010x`
    with a Unicode hyphen, would pass them while LOOKING like CD-bot's. So the
    name is folded to a skeleton (NFKC, dash look-alikes to `-`, Cyrillic and
    Greek confusables to Latin). When the skeleton starts with a full two-part
    prefix (`XX-agent-`) or with ``own_prefix``, and the original differs from
    the skeleton there, the first differing character is returned.

    A name that does not read as a prefix once folded is left alone, whatever
    it contains: `Caf\u00e9-Paris-promo` and `Summer \u2013 Sale` are plain names.
    """
    text = str(name or "")
    pieces = [_fold(c) for c in text]
    skeleton = "".join(pieces)
    ends = []
    match = _SKELETON_PREFIX.match(skeleton)
    if match:
        ends.append(match.end())
    if own_prefix and skeleton.startswith(own_prefix):
        ends.append(len(own_prefix))
    if not ends:
        return None
    end, position = max(ends), 0
    for c, piece in zip(text, pieces):
        if position >= end:
            break
        if piece != c:
            return c
        position += len(piece)
    return None


class NameRefused(ValueError):
    """A requested name that cannot be used as asked; the message says why."""


@dataclass(frozen=True)
class Identity:
    """Who this checkout's creations are attributed to. Either part may be empty."""

    owner: str = ""
    agent: str = ""
    #: Where the values came from: ".env", "config.env", both, or "" when unset.
    source: str = ""
    #: One line per malformed value, each naming its `.env` key.
    problems: tuple[str, ...] = ()
    #: The CLI's identity: the owner's initials with `cli` as the agent part.
    is_cli: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.owner or self.agent)

    @property
    def prefix(self) -> str:
        """`AB-max-`, `AB-`, `max-`, or "" -- built from the VALID parts only."""
        parts = [p for p in (self.owner, self.agent) if p]
        return "-".join(parts) + "-" if parts else ""

    @classmethod
    def from_values(cls, owner: str | None, agent: str | None, source: str = "") -> "Identity":
        owner, agent = (owner or "").strip(), (agent or "").strip()
        problems = []
        if owner and not OWNER_RE.fullmatch(owner):
            problems.append(
                f"{OWNER_KEY} in .env is {owner!r}: it must be 2-4 uppercase letters "
                f"(for example AB). Fix it, or leave it empty for no initials."
            )
            owner = ""
        if agent and (not AGENT_RE.fullmatch(agent) or agent in RESERVED_AGENTS):
            why = (
                f"{agent!r} is reserved"
                if agent in RESERVED_AGENTS
                else f"{agent!r}: it must be a lowercase slug of 2-24 letters, digits "
                f"or _, starting with a letter, with no '-' (for example max)"
            )
            problems.append(
                f"{AGENT_KEY} in .env is {why}. Fix it, or leave it empty for no agent name."
            )
            agent = ""
        return cls(owner=owner, agent=agent, source=source, problems=tuple(problems))

    @classmethod
    def load(cls, files: list[Path] | None = None) -> "Identity":
        """From `config.env` then `.env` (a later file wins). Never the process env."""
        values = read_env_files(files)
        sources = [name for name, _ in values.get("_sources", [])]
        return cls.from_values(
            values.get(OWNER_KEY), values.get(AGENT_KEY), source=" + ".join(sources)
        )

    def cli(self) -> "Identity":
        """`<OWNER>-cli-` when the initials are set; no prefix at all when not.

        The `cli` marker tells a human from an agent under the same owner. With
        no owner there is nothing to mark, so the name is used as given.
        """
        return Identity(
            owner=self.owner, agent=CLI_AGENT if self.owner else "", source=self.source,
            problems=tuple(p for p in self.problems if p.startswith(OWNER_KEY)), is_cli=True,
        )

    def owns(self, name: str | None) -> bool:
        """True when ``name`` carries this identity's prefix (and one is set).

        Starting with the prefix is not enough: with only the initials set,
        `AB-` starts `AB-bot-x` (another agent's) and `AB-cli-x` (a human's
        CLI creation). A name that parses as a full two-part prefix is ours
        only when BOTH parts are this identity's.
        """
        text = str(name or "")
        if not self.prefix or not text.startswith(self.prefix):
            return False
        if lookalike_in_prefix(text, self.prefix) is not None or invisible_character(text) is not None:
            return False  # looks like someone else's, or hides something
        parsed = parse(text)
        return parsed is None or (parsed.owner, parsed.agent) == (self.owner, self.agent)

    def describe(self) -> dict[str, Any]:
        """For get_guardrails / whoami: which parts are set, and what they make."""
        return {
            "owner_initials": self.owner or None,
            "agent_name": self.agent or None,
            "prefix": self.prefix,
            "source": self.source or None,
            "configured": self.configured,
            "problems": list(self.problems),
            "keys": [OWNER_KEY, AGENT_KEY],
            "rule": RULES["none"] if not self.configured else None,
        }

    def ledger_record(self) -> dict[str, Any]:
        return {"owner": self.owner or None, "agent": self.agent or None,
                "source": self.source or None}


def read_env_files(files: list[Path] | None = None) -> dict[str, Any]:
    """The identity keys from each env file that exists, later files winning.

    Only a NON-EMPTY value wins: `APPLE_ADS_AGENT_NAME=` in `.env` leaves a
    value from `config.env` in place rather than erasing it.
    """
    merged: dict[str, Any] = {"_sources": []}
    for path in ENV_FILES if files is None else files:
        if not path.exists():
            continue
        values = dotenv_values(path)
        found = {
            k: str(values.get(k)).strip()
            for k in (OWNER_KEY, AGENT_KEY)
            if str(values.get(k) or "").strip()
        }
        if found:
            merged["_sources"].append((path.name, found))
        merged.update(found)
    return merged


def process_env_warnings(identity: Identity, environ: dict[str, str] | None = None) -> list[str]:
    """A startup warning per identity key set in the PROCESS env that disagrees
    with `.env`. The process env is ignored; this only makes the mismatch visible."""
    environ = os.environ if environ is None else environ
    warnings = []
    raw = read_env_files()
    for key in (OWNER_KEY, AGENT_KEY):
        if key not in environ:
            continue
        if (environ.get(key) or "").strip() != (raw.get(key) or "").strip():
            warnings.append(
                f"{key} is set in the process environment and differs from .env. The "
                f"process environment is ignored for naming; .env is the only source."
            )
    return warnings


@dataclass(frozen=True)
class Parsed:
    owner: str | None
    agent: str | None
    base: str
    origin: str  # agent | cli | manual


def parse(name: str | None) -> Parsed | None:
    """`OWNER-agent-base` -> its parts; None for any other shape."""
    match = FULL_PREFIX.fullmatch(str(name or ""))
    if not match:
        return None
    owner, agent, base = match.groups()
    return Parsed(owner, agent, base, "cli" if agent == CLI_AGENT else "agent")


def classify(name: str | None, identity: Identity) -> Parsed:
    """Where a name says it came from. A convention, not proof: the ledger is.

    A full two-part prefix parses on its own. A one-part prefix is recognised
    only when it is THIS checkout's. Anything else is manual.
    """
    text = str(name or "")
    parsed = parse(text)
    if parsed is not None:
        return parsed
    if identity.owns(text):
        return Parsed(
            identity.owner or None, identity.agent or None, text[len(identity.prefix):],
            "cli" if identity.is_cli else "agent",
        )
    return Parsed(None, None, text, "manual")


RULES = {
    "stamped": "prefix added",
    "already_ours": "the name already carried this prefix; it was not added twice",
    "kept_prefix": "rename of our own entity; the prefix is kept",
    "restored_prefix": "rename of an entity the ledger shows we created; restoring the prefix",
    "none": "no identity configured; name used as given",
}


@dataclass
class Stamped:
    """The exact name that will be sent, and how it was arrived at."""

    final: str
    base: str
    requested: str
    prefix: str
    rule: str
    max_length: int
    #: (check name, detail) for each reason the apply must not run.
    failures: list[tuple[str, str]] = field(default_factory=list)

    def block(self) -> dict[str, Any]:
        """The preview's `naming` block."""
        return {
            "requested": self.requested,
            "final": self.final,
            "prefix": self.prefix,
            "base": self.base,
            "length": len(self.final),
            "max_length": self.max_length,
            "rule": RULES.get(self.rule, self.rule),
        }

    def checks(self) -> list[Any]:
        """The guardrail lines a preview carries: a FAIL per failure, else one OK."""
        from apple_ads_mcp.guardrails import Check

        if self.failures:
            return [Check(name, False, detail) for name, detail in self.failures]
        return [Check("name", True, f"{self.final!r} ({len(self.final)}/{self.max_length} characters)")]

    def item_fields(self, identity: "Identity") -> dict[str, Any]:
        """What the ledger row keeps: who it was named for, and what was asked."""
        return {"identity": identity.ledger_record(), "requested_name": self.requested}

    def warning(self) -> str | None:
        if self.final == self.requested:
            return None
        return f'the name will be "{self.final}" ({RULES.get(self.rule, self.rule)})'


def stamp(identity: Identity, requested: str | None, *, entity_type: str) -> Stamped:
    """The final name for something about to be CREATED.

    Raises NameRefused for a name that cannot be used as asked (empty, control
    characters, another agent's prefix). A malformed identity or an over-length
    result is not raised: it comes back in ``failures`` so the preview can show
    the exact name and the reason side by side, and the apply is blocked.
    """
    text = str(requested or "").strip()
    if not text:
        raise NameRefused("INVALID_REQUEST: the name is empty")
    other = parse(text)
    if other is not None and (other.owner, other.agent) != (identity.owner, identity.agent):
        # Refused whatever is configured -- with no identity at all too: taken
        # verbatim, the name would claim to be that identity's creation.
        raise NameRefused(
            f"INVALID_REQUEST: the name starts with {other.owner}-{other.agent}-, which is "
            f"another agent's prefix (or reads as one), and this checkout's is "
            f"{identity.prefix or 'empty'}. Using it would impersonate "
            f"that identity. Pass the base name only; if the base itself starts like "
            f"that, reorder it."
        )
    prefix = identity.prefix
    base, rule = text, "stamped" if prefix else "none"
    if prefix and text.startswith(prefix):
        base, rule = text[len(prefix):].strip(), "already_ours"
        if not base:
            raise NameRefused(f"INVALID_REQUEST: the name is only the prefix {prefix!r}; give a base name")
        if base.startswith(prefix):
            raise NameRefused(
                f"INVALID_REQUEST: the name carries the prefix {prefix!r} twice; pass the "
                f"base name only and it is added once."
            )
        inner = parse(base)
        if inner is not None:
            raise NameRefused(
                f"INVALID_REQUEST: after the prefix {prefix!r}, the name starts with "
                f"{inner.owner}-{inner.agent}-, which reads as another prefix. Pass the base "
                f"name only; if the base itself starts like that, reorder it."
            )
    final = prefix + base
    bad = invisible_character(final)
    if bad is not None:
        raise NameRefused(
            f"INVALID_REQUEST: the name contains a control character or other invisible "
            f"character (U+{ord(bad):04X} {unicodedata.name(bad, 'unnamed')}); tabs, newlines, NUL, "
            f"zero-width and bidi marks, line separators and blank fillers are refused. "
            f"Remove it."
        )
    look = lookalike_in_prefix(final, prefix)
    if look is not None:
        raise NameRefused(
            f"INVALID_REQUEST: {final!r} reads as a prefix (XX-agent- or this checkout's "
            f"{prefix or 'none'}) through U+{ord(look):04X} "
            f"{unicodedata.name(look, 'unnamed')}, a look-alike letter or dash. It could "
            f"pass for another identity's name while the checks read a plain one. Use "
            f"ASCII letters and '-' there, or reorder the name."
        )
    claimed = parse(final)
    if claimed is not None and (claimed.owner, claimed.agent) != (identity.owner, identity.agent):
        # Only the initials are set and the base begins `max-...`: the result
        # would read as agent `max`'s, which this checkout is not.
        raise NameRefused(
            f"INVALID_REQUEST: {final!r} would read as {claimed.owner}-{claimed.agent}-'s, "
            f"which is not this checkout's identity ({prefix or 'no prefix'}). Reorder "
            f"the base so it does not start with a lowercase word and '-'."
        )
    limit = NAME_LIMITS.get(entity_type, MAX_NAME_LENGTH)
    failures = [("agent_identity", problem) for problem in identity.problems]
    if len(final) > limit:
        failures.append((
            "name_length",
            f"the name would be {len(final)} characters and the cap is {limit}; the base "
            f"may be at most {limit - len(prefix)} characters and it is {len(base)}. It is "
            f"refused rather than cut short -- shorten the base and preview again.",
        ))
    return Stamped(final=final, base=base, requested=text, prefix=prefix, rule=rule,
                   max_length=limit, failures=failures)


def rename(
    identity: Identity, current: str | None, requested: str | None, *,
    entity_type: str, ledger_owned: bool,
) -> Stamped:
    """The final name for a RENAME, which the agent may do only to its own entities.

    Its own: the live name carries this checkout's prefix, or the ledger shows
    this server created it. Anything else -- a hand-made entity, another
    agent's -- is refused, with no override. The prefix is kept (or, for a
    ledger-owned entity that lost it in the UI, restored); only the base changes.

    A live name that parses as a FULL identity other than ours is refused even
    when the ledger shows a creation: the name says it is someone else's now,
    and renaming would re-stamp it as ours.
    """
    current = str(current or "")
    other = parse(current)
    foreign = other is not None and (other.owner, other.agent) != (identity.owner, identity.agent)
    if foreign or not (identity.owns(current) or ledger_owned):
        whose = (
            f"its name carries {other.owner}-{other.agent}-, another agent's prefix"
            if other is not None and current
            else "its name does not carry this agent's prefix"
            + (f" ({identity.prefix})" if identity.prefix else "")
        )
        ledger_note = (
            "whatever the ledger shows, a name that reads as another identity's is not "
            "this agent's"
            if foreign
            else "and the ledger shows no creation of it by this server"
        )
        raise NameRefused(
            f"INVALID_REQUEST: {entity_type.replace('_', ' ')} {current!r} is not this "
            f"agent's to rename: {whose}, {ledger_note}. The agent may rename only what "
            f"it created. Rename it in the Apple Ads UI instead."
        )
    stamped = stamp(identity, requested, entity_type=entity_type)
    if identity.prefix:
        stamped.rule = "kept_prefix" if identity.owns(current) else "restored_prefix"
    return stamped


# --- filtering the list tools -----------------------------------------------------

ORIGINS = ("any", "agent", "cli", "manual")


def attribution(name: str | None, identity: Identity, *, ledger_created: bool | None = None) -> dict[str, Any]:
    """The fields every listed row with a name carries."""
    parsed = classify(name, identity)
    record = {"origin": parsed.origin, "owner": parsed.owner, "agent": parsed.agent,
              "base": parsed.base if parsed.origin != "manual" else None}
    if ledger_created is not None:
        record["ledger_created"] = ledger_created
    return record


def name_filter(
    identity: Identity, origin: str = "any", agent_name: str | None = None,
    owner: str | None = None, mine: bool = False,
):
    """A predicate over names for the list tools, or None when nothing filters.

    Each value is validated with the same rules as the identity (so nothing
    unchecked reaches a comparison), and raises NameRefused when it is not.
    A one-part prefix (`AB-...`, `max-...`) is recognised only as this
    checkout's own; owner/agent filters match full two-part names and ours.
    """
    origin = (origin or "any").strip().lower()
    if origin not in ORIGINS:
        raise NameRefused(f"INVALID_REQUEST: origin must be one of {', '.join(ORIGINS)}")
    agent_name = (agent_name or "").strip() or None
    owner = (owner or "").strip() or None
    if agent_name is not None and not AGENT_RE.fullmatch(agent_name):
        raise NameRefused(f"INVALID_REQUEST: agent_name {agent_name!r} is not a lowercase slug like max")
    if owner is not None and not OWNER_RE.fullmatch(owner):
        raise NameRefused(f"INVALID_REQUEST: owner {owner!r} is not 2-4 uppercase letters like AB")
    if mine and not identity.configured:
        raise NameRefused(
            f"INVALID_REQUEST: mine=True needs an identity, and none is configured: set "
            f"{OWNER_KEY} and/or {AGENT_KEY} in .env."
        )
    if origin == "any" and agent_name is None and owner is None and not mine:
        return None

    def keep(name: str | None) -> bool:
        parsed = classify(name, identity)
        if origin != "any" and parsed.origin != origin:
            return False
        if agent_name is not None and parsed.agent != agent_name:
            return False
        if owner is not None and parsed.owner != owner:
            return False
        if mine and not identity.owns(name):
            return False
        return True

    return keep
