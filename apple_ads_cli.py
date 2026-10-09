#!/usr/bin/env python3
"""Break-glass CLI for the Apple Ads Platform API: any endpoint, driven by a human.

The MCP server in `apple_ads_mcp/` is the normal path, and it deliberately
exposes a narrow surface -- no creates, no deletes, no generic passthrough. This
script is the other half of that bargain: when you need something the server
refuses to expose (create a campaign, delete a negative keyword, call an
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

Paths are relative to https://api.ads.apple.com/v1 and the context header is
added for you. `/acls` and `/me` are the two endpoints that must NOT carry it,
and this script leaves it off for those.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

from _bootstrap import ensure_venv

ensure_venv()  # re-exec under venv/ if launched with a bare `python3`

import urllib3

from apple_ads_mcp import logdb
from apple_ads_mcp.client import context_header, get_api
from apple_ads_mcp.config import ConfigError, resolve_ad_account_id

BASE_URL = "https://api.ads.apple.com/v1"

# Every read-only POST on this API ends in /query -- campaigns/query,
# reports/apps/campaigns/query, change-history/query and so on. That is a far
# cleaner rule than v5's, which needed both a `reports/` prefix and a `/find`
# suffix to describe the same idea. Verified against all 80 resource paths in
# apple-ads-platform 1.109.0 on 2026-10-09.
READ_ONLY_POST_SUFFIX = "/query"

TIMEOUT = 60.0


class CliError(RuntimeError):
    """Something the operator can fix, printed without a traceback."""


def is_mutation(method: str, path: str) -> bool:
    """True when this request would CHANGE something at Apple's end.

    Method alone is not a reliable signal: this API uses POST for every
    query-by-selector and every report. Classify by method AND path.
    """
    method = method.upper()
    if method in ("PUT", "PATCH", "DELETE"):
        return True
    if method != "POST":
        return False
    return not path.strip("/").lower().endswith(READ_ONLY_POST_SUFFIX)


# `/acls` and `/me` take no account context -- they answer "who is this
# credential and what can it reach", which is the question you ask when the
# context itself is what you doubt. Note the path is `/acls`, NOT `/me/acls`.
NO_CONTEXT_PATHS = frozenset({"me", "acls"})


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

    status, payload = request(args.method, args.path, body, token)

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
