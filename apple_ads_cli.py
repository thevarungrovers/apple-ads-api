#!/usr/bin/env python3
"""Break-glass CLI for the Apple Ads Platform API: any endpoint, driven by a human.

The MCP server in `apple_ads_mcp/` is the normal path, and it deliberately
exposes a narrow surface -- typed preview/apply pairs, no deletes, no generic
passthrough. This script is the other half of that bargain: when you need
something the server refuses to expose (delete a negative keyword, call an
endpoint nobody has wrapped), you reach for this instead of widening the agent's
surface.

The distinction that makes that safe is WHO IS DRIVING. The MCP server is
reachable by an agent and therefore has to assume the caller may be wrong. This
is a shell command: it exists in a human's scrollback, under a human's hands,
and its guard is the one an agent could trivially defeat -- a flag.

  ./venv/bin/python apple_ads_cli.py campaigns/query -X POST -d '{}'
  ./venv/bin/python apple_ads_cli.py campaigns/1234567890
  ./venv/bin/python apple_ads_cli.py campaigns/1234567890 -X PUT \\
      -d '{"status":"PAUSED"}' --apply --confirm-live

NOTHING IS SENT WITHOUT --apply. A mutation without it prints the exact request
and exits. That is the same contract the retired v5 CLI had, and the reason to
keep it: the dry run is the thing you read before you mean it.

--apply IS FOR HUMANS ONLY. A flag is a guard an agent can defeat by typing it,
so --apply also requires an interactive terminal (stdin is a TTY) and the path
typed back at a prompt. An agent's shell has no TTY, so it cannot send a write
from here; the MCP server tells it to hand the command to a human instead, and
install.py adds a Claude Code deny rule for `apple_ads_cli.py ... --apply`. The
kill switch (.audit/DISABLE_WRITES) stops this CLI too, and every write it
sends gets a row in the `changes` ledger (tool="cli"), so the daily cap and
reconcile_ledger see it.

NAMES GET THE CLI PREFIX. A POST that creates a campaign, ad group, ad or
creative, or a PUT to one by id, with a "name" in its body has that name stamped
`<OWNER>-cli-<base>` (apple_ads_mcp/naming.py) when APPLE_ADS_OWNER_INITIALS is
set in .env, and left exactly as given when it is not -- `cli` tells a human
from an agent under the same owner, so with no owner there is nothing to mark.
The dry run prints the final body, the confirmation shows the final name, and
the ledger row records the identity. Another agent's prefix is refused; there
is no --no-prefix (a person who wants an unprefixed name uses the Apple Ads UI).

Paths are relative to https://api.ads.apple.com/v1 and the context header is
added for you. `/acls` and `/me` are the two endpoints that must NOT carry it,
and this script leaves it off for those.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.parse

from _bootstrap import ensure_venv

ensure_venv()  # re-exec under venv/ if launched with a bare `python3`

import urllib3

from apple_ads_mcp import ledger, logdb
from apple_ads_mcp.client import context_header, get_api
from apple_ads_mcp.config import ConfigError, resolve_ad_account_id
from apple_ads_mcp.guardrails import writes_disabled
from apple_ads_mcp.naming import Identity, NameRefused, stamp

BASE_URL = "https://api.ads.apple.com/v1"

# Every read-only POST on this API ends in /query -- campaigns/query,
# reports/apps/campaigns/query, change-history/query and so on. That is a far
# cleaner rule than v5's, which needed both a `reports/` prefix and a `/find`
# suffix to describe the same idea. Verified against all 80 resource paths in
# apple-ads-platform 1.109.0 on 2026-10-09.
#
# The rule is applied FAIL-CLOSED, to the whole path rather than its tail: a
# POST is a read only when every segment is a plain resource name or id and the
# last one is `query`. Anything else -- a query string or fragment (which is
# sent verbatim, so `campaigns?x=/query` would end in /query and still create
# a campaign), percent-encoding, `.`/`..`, an empty path -- is a mutation.
#
# re.ASCII as well: without it IGNORECASE folds non-ASCII letters onto ASCII
# ones (the long s, U+017F, matches "s"), so `campaignſ/query` would pass for a
# read and be sent verbatim.
READ_ONLY_POST_SHAPE = re.compile(r"(?:[a-z0-9_-]+/)+query", re.IGNORECASE | re.ASCII)

#: Anything outside printable ASCII without the space: whitespace, control
#: characters and non-ASCII. urlsplit silently DROPS a tab, CR or LF and strips
#: leading spaces, so it cannot be trusted to see them -- they are refused first.
_UNSAFE_PATH_CHAR = re.compile(r"[^\x21-\x7e]")

# Methods that never change anything. Every other method, including ones this
# script has never heard of, is treated as a write.
READ_METHODS = frozenset({"GET", "HEAD"})

TIMEOUT = 60.0


class CliError(RuntimeError):
    """Something the operator can fix, printed without a traceback."""


def is_mutation(method: str, path: str) -> bool:
    """True when this request would CHANGE something at Apple's end.

    Method alone is not a reliable signal: this API uses POST for every
    query-by-selector and every report. Classify by method AND path, and when
    in doubt, call it a mutation -- the cost of a wrong "write" is a dry run,
    the cost of a wrong "read" is an unguarded live write.
    """
    method = (method or "").strip().upper()
    if method in READ_METHODS:
        return False
    if method != "POST":
        return True
    if _UNSAFE_PATH_CHAR.search(path or ""):
        return True
    if "?" in path or "#" in path:
        return True
    split = urllib.parse.urlsplit(path)
    if split.scheme or split.netloc or split.query or split.fragment:
        return True
    return READ_ONLY_POST_SHAPE.fullmatch(split.path.strip("/")) is None


# `/acls` and `/me` take no account context -- they answer "who is this
# credential and what can it reach", which is the question you ask when the
# context itself is what you doubt. Note the path is `/acls`, NOT `/me/acls`.
NO_CONTEXT_PATHS = frozenset({"me", "acls"})


#: Collections whose entities carry a name, as the path spells them.
NAMED_COLLECTIONS = {
    "campaigns": "campaign",
    "adgroups": "ad_group",
    "ads": "ad",
    "creatives": "creative",
}


def named_entity(method: str, path: str) -> str | None:
    """The entity type when this request creates (POST to the collection) or
    updates (PUT to `<collection>/<id>`) a named entity; None otherwise."""
    method = (method or "").strip().upper()
    parts = [p.lower() for p in path.strip("/").split("/") if p]
    if method == "POST" and parts and parts[-1] in NAMED_COLLECTIONS:
        return NAMED_COLLECTIONS[parts[-1]]
    if method == "PUT" and len(parts) >= 2 and parts[-2] in NAMED_COLLECTIONS and parts[-1].isdigit():
        return NAMED_COLLECTIONS[parts[-2]]
    return None


def cli_identity() -> Identity:
    """`<OWNER>-cli-` from .env, or no prefix when the initials are empty."""
    return Identity.load().cli()


def stamp_body(method: str, path: str, body: dict | None, identity: Identity):
    """(body with its name stamped, the Stamped result or None). Raises CliError.

    Only a create or an update of a named entity with a string "name" is
    touched; every other request is sent exactly as given.
    """
    entity_type = named_entity(method, path)
    if entity_type is None or not isinstance(body, dict) or not isinstance(body.get("name"), str):
        return body, None
    try:
        stamped = stamp(identity, body["name"], entity_type=entity_type)
    except NameRefused as exc:
        raise CliError(str(exc)) from None
    if stamped.failures:
        raise CliError("; ".join(detail for _name, detail in stamped.failures))
    return {**body, "name": stamped.final}, stamped


def needs_context(path: str) -> bool:
    return path.strip("/").lower() not in NO_CONTEXT_PATHS


def load_body(raw: str | None) -> dict | None:
    """`-d '{...}'` or `-d @file.json`."""
    if raw is None:
        return None
    if raw.startswith("@"):
        path = pathlib.Path(raw[1:])
        if not path.exists():
            raise CliError(f"{path} not found")
        raw = path.read_text()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"-d is not valid JSON: {exc}") from None


def serving_campaign_id(path: str) -> str | None:
    """The campaign id in a path like `campaigns/123`, when there is one.

    Used to decide whether --confirm-live is required. `campaigns/query` and
    `campaigns` (a create) match no existing campaign, so neither is gated.
    """
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0].lower() == "campaigns" and parts[1].isdigit():
        return parts[1]
    return None


def _input_stream():
    """Where the confirmation is read from. A function so tests can swap it."""
    return sys.stdin


def confirm_apply(path: str, *, stdin=None, ask=None) -> str | None:
    """Why this --apply must not go ahead, or None when a human confirmed it.

    Three gates, all of which an agent's non-interactive shell fails:
      - the kill switch, which stops every write path including this one;
      - an interactive terminal -- stdin must be a TTY;
      - the path, typed back exactly at a prompt.
    """
    reason = writes_disabled()
    if reason:
        return f"writes are disabled: {reason}. Remove .audit/DISABLE_WRITES to re-enable them."
    stdin = _input_stream() if stdin is None else stdin
    ask = ask or input
    try:
        interactive = stdin.isatty()
    except (AttributeError, ValueError):
        interactive = False
    if not interactive:
        return (
            "--apply needs an interactive terminal and a typed confirmation. This "
            "CLI is the break-glass path for a HUMAN; an agent must hand the "
            "command over rather than run it."
        )
    wanted = path.strip("/")
    try:
        typed = ask(f"Live account. Type the path to confirm ({wanted}): ").strip().strip("/")
    except EOFError:
        typed = ""
    if typed != wanted:
        return f"confirmation did not match ({typed!r} is not {wanted!r}); nothing was sent."
    return None


def ledger_target(path: str) -> tuple[str, str]:
    """(entity_type, entity_id) for the ledger row, from a path like `campaigns/123`.

    A guess from a URL, labelled as one: the row exists so the daily cap and
    reconcile_ledger can SEE the write, not so revert_change can undo it -- its
    field is the HTTP method and path, which no change kind owns, so a revert
    of a CLI row is refused.
    """
    parts = [p for p in path.strip("/").split("/") if p]
    entity_type = f"cli:{parts[0]}" if parts else "cli"
    numeric = [p for p in parts if p.isdigit()]
    return entity_type, numeric[-1] if numeric else "(new)"


def request(method: str, path: str, body: dict | None, token: str) -> tuple[int, str]:
    """Send one raw request, and log it.

    This path does NOT go through `apple_ads_mcp.client.call()`, so it would be
    invisible to a log that only instrumented the SDK -- and it is the one path
    with no guardrails in front of it, which makes it the path whose record
    matters most. It writes an `api_calls` row, not a `changes` row: a generic
    endpoint caller cannot know which field of which entity it just moved, and
    a ledger entry guessed from a URL would be worse than none.
    """
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if needs_context(path):
        headers["X-AP-Context"] = context_header()
    with logdb.record_api_call(method.upper(), path=path, request=body) as slot:
        response = urllib3.PoolManager(
            timeout=urllib3.Timeout(connect=10.0, read=TIMEOUT)
        ).request(
            method.upper(),
            f"{BASE_URL}/{path.strip('/')}",
            body=json.dumps(body).encode() if body is not None else None,
            headers=headers,
        )
        slot["http_status"] = response.status
        return response.status, response.data.decode("utf-8", "replace")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Call any Apple Ads Platform API endpoint. Writes need --apply.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("path", help="path under /v1, e.g. campaigns/query")
    parser.add_argument("-X", "--method", default="GET", help="HTTP method (default: %(default)s)")
    parser.add_argument("-d", "--data", default=None, help="JSON body, or @file.json")
    parser.add_argument("--apply", action="store_true",
                        help="actually send a mutating request (without it, nothing is sent)")
    parser.add_argument("--confirm-live", action="store_true",
                        help="required as well as --apply to write to a campaign that is "
                             "serving right now")
    args = parser.parse_args()

    label = f"apple_ads_cli {args.method.upper()} {args.path.strip('/')}"
    with logdb.record_tool_call(
        label,
        args={
            "method": args.method.upper(),
            "path": args.path,
            "apply": args.apply,
            "confirm_live": args.confirm_live,
        },
    ):
        return _run(args)


def _run(args: argparse.Namespace) -> int:
    """Everything after argument parsing, so one command is one tool call.

    A single invocation can send two requests -- the serving-campaign pre-check
    GET and then the write itself -- and grouping them under one `tool_calls`
    row is what makes the log read as "this command did this" rather than as
    two unrelated requests that happened close together.
    """
    try:
        body = load_body(args.data)
        account = resolve_ad_account_id()
        api = get_api()
        token = api.api_client._access_token_provider.get_token()
    except (CliError, ConfigError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    mutation = is_mutation(args.method, args.path)
    stamped = None
    identity = cli_identity()
    if mutation:
        try:
            body, stamped = stamp_body(args.method, args.path, body, identity)
        except CliError as exc:
            print(f"error: {exc} Nothing was sent.", file=sys.stderr)
            return 1
    if stamped is not None:
        print(
            f"naming: {stamped.requested!r} -> {stamped.final!r} "
            f"(prefix {stamped.prefix!r} from .env; {stamped.block()['rule']})",
            file=sys.stderr,
        )

    if mutation and not args.apply:
        print("DRY RUN — nothing was sent.\n")
        print(f"  {args.method.upper()} {BASE_URL}/{args.path.strip('/')}")
        print(f"  X-AP-Context: {context_header() if needs_context(args.path) else '(none)'}")
        if body is not None:
            print("  body:")
            for line in json.dumps(body, indent=2).splitlines():
                print(f"    {line}")
        print("\nAdd --apply to send it.")
        return 0

    if mutation and args.apply:
        if stamped is not None:
            print(f"The name that will be sent: {stamped.final!r}", file=sys.stderr)
        refusal = confirm_apply(args.path)
        if refusal:
            print(f"error: {refusal}", file=sys.stderr)
            return 1

    # Before writing to a NAMED campaign, read it back and refuse if it is
    # serving. Reading first costs one GET and is the difference between
    # "I meant to write" and "I meant to write to something spending money now".
    if mutation and args.apply:
        campaign_id = serving_campaign_id(args.path)
        if campaign_id and not args.confirm_live:
            status, payload = request("GET", f"campaigns/{campaign_id}", None, token)
            if status == 200:
                try:
                    display = ((json.loads(payload) or {}).get("result") or {}).get("displayStatus")
                except json.JSONDecodeError:
                    display = None
                if display == "RUNNING":
                    print(
                        f"error: campaign {campaign_id} is RUNNING — it is spending money "
                        f"today.\n       Add --confirm-live if that is what you meant.",
                        file=sys.stderr,
                    )
                    return 1

    entry_id = None
    if mutation:
        # The intent goes in BEFORE the request, exactly as on the MCP path: a
        # crash between the two leaves an open intent for reconcile_ledger.
        entity_type, entity_id = ledger_target(args.path)
        entry_id = ledger.new_entry_id()
        campaign_id = serving_campaign_id(args.path)
        ledger.write_intent(
            entry_id,
            tool="cli",
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=None,
            path=args.path.strip("/"),
            field=f"{args.method.upper()} {args.path.strip('/')}",
            before=None,
            after=json.dumps(body)[:2000] if body is not None else "",
            campaign_id=int(campaign_id) if campaign_id else None,
            projected_daily_spend_delta="0",
            extra={
                "source": "apple_ads_cli", "unbounded": True,
                **({"identity": identity.ledger_record(), "requested_name": stamped.requested}
                   if stamped is not None else {}),
            },
        )

    try:
        status, payload = request(args.method, args.path, body, token)
    except Exception as exc:
        if entry_id:
            # No HTTP status came back, so the request may have reached Apple
            # and landed: unknown, not failed, so the daily cap keeps counting
            # it and reconcile_ledger lists it.
            ledger.write_outcome(
                entry_id,
                outcome=ledger.UNKNOWN,
                detail=f"no response, may have landed: {exc}"[:500],
            )
            print(
                f"error: no response from Apple ({exc}). The write MAY HAVE LANDED; "
                f"check the account (reconcile_ledger, or the Apple Ads UI) before "
                f"running it again.",
                file=sys.stderr,
            )
        raise

    if entry_id:
        # A 5xx is Apple failing, not Apple refusing: the write may have been
        # applied before the error, exactly like a timeout. UNKNOWN, as the MCP
        # path records a TRANSIENT error, so the daily cap keeps counting it.
        if status >= 500:
            outcome, detail = ledger.UNKNOWN, f"HTTP {status}: outcome unknown, may have landed"
        else:
            outcome, detail = (ledger.APPLIED if status < 400 else ledger.FAILED), f"HTTP {status}"
        ledger.write_outcome(
            entry_id,
            outcome=outcome,
            observed_after=payload[:2000],
            detail=detail,
        )
        if status >= 500:
            print(
                f"error: Apple answered HTTP {status}. The write MAY HAVE LANDED; check "
                f"the account (reconcile_ledger, or the Apple Ads UI) before running it "
                f"again.",
                file=sys.stderr,
            )

    if mutation:
        print(f"# {args.method.upper()} {args.path} on ad account {account} -> HTTP {status}",
              file=sys.stderr)
    try:
        print(json.dumps(json.loads(payload), indent=2))
    except json.JSONDecodeError:
        print(payload)
    return 0 if status < 400 else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        raise SystemExit(130)
