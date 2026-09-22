#!/usr/bin/env python3
"""Exchange the client-secret JWT for an Apple Search Ads access token.

Access tokens live 3600 seconds. This module caches one in .token_cache.json
(mode 600) and refreshes it automatically once it is within REFRESH_MARGIN of
expiry, so callers can just ask for a token every time.

Two expiries are in play and they are easy to confuse:
  - the ACCESS TOKEN, 3600s, refreshed automatically here;
  - the CLIENT SECRET, up to 180 days, re-signed on demand by
    generate_client_secret.py but never renewed by Apple.
A sudden `invalid_client` after months of working code is almost always the
second one.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import sys

from _bootstrap import ensure_venv

ensure_venv()  # re-exec under venv/ if launched with a bare `python3`

import requests

from generate_client_secret import ConfigError, build_client_secret, load_config

TOKEN_URL = "https://appleid.apple.com/auth/oauth2/token"
SCOPE = "searchadsorg"

HERE = pathlib.Path(__file__).resolve().parent
CACHE_FILE = HERE / ".token_cache.json"

# Refresh slightly early so a token cannot expire in flight on a slow request.
REFRESH_MARGIN = dt.timedelta(seconds=120)
DEFAULT_EXPIRES_IN = 3600


class TokenError(RuntimeError):
    """The token endpoint refused us."""


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _read_cache() -> dict | None:
    """Return the cached token, or None if absent/unreadable/malformed."""
    if not CACHE_FILE.exists():
        return None
    try:
        cached = json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None  # a corrupt cache is just a cache miss

    if not cached.get("access_token") or not cached.get("expires_at"):
        return None
    try:
        cached["expires_at_dt"] = dt.datetime.fromisoformat(cached["expires_at"])
    except (TypeError, ValueError):
        return None
    return cached


def _write_cache(payload: dict) -> None:
    """Write the cache at mode 600 from the start -- never world-readable, even briefly."""
    serialised = json.dumps(payload, indent=2) + "\n"
    fd = os.open(CACHE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(serialised)


def clear_cache() -> bool:
    if CACHE_FILE.exists():
        CACHE_FILE.unlink()
        return True
    return False


def fetch_new_token(config: dict[str, str] | None = None) -> dict:
    """Sign a fresh client secret, trade it for an access token, cache the result."""
    config = config or load_config()
    client_secret, _ = build_client_secret(config=config)

    response = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "client_credentials",
            "client_id": config["APPLE_ADS_CLIENT_ID"],
            "client_secret": client_secret,
            "scope": SCOPE,
        },
        headers={
            "Host": "appleid.apple.com",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=30,
    )

    if response.status_code != 200:
        raise TokenError(_explain_failure(response))

    body = response.json()
    if not body.get("access_token"):
        raise TokenError(f"HTTP 200 but no access_token in response: {body}")

    obtained_at = _now()
    expires_in = int(body.get("expires_in") or DEFAULT_EXPIRES_IN)
    cached = {
        "access_token": body["access_token"],
        "token_type": body.get("token_type", "Bearer"),
        "scope": body.get("scope", SCOPE),
        "expires_in": expires_in,
        "obtained_at": obtained_at.isoformat(),
        "expires_at": (obtained_at + dt.timedelta(seconds=expires_in)).isoformat(),
    }
    _write_cache(cached)
    cached["expires_at_dt"] = dt.datetime.fromisoformat(cached["expires_at"])
    return cached


def _explain_failure(response: requests.Response) -> str:
    """Turn Apple's terse OAuth errors into something actionable."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    error = body.get("error", "")
    message = f"token endpoint returned HTTP {response.status_code}: {body or response.text[:400]}"

    if error == "invalid_client":
        message += (
            "\n\n`invalid_client` almost always means one of:"
            "\n  1. the client secret is past its 180-day exp -- check it with:"
            "\n       ./venv/bin/python generate_client_secret.py"
            "\n     (a fresh run re-signs it; no trip to ads.apple.com needed)"
            "\n  2. sub/iss are swapped -- sub must be the clientId, iss the teamId"
            "\n     (they are usually the same string, so this looks correct either way)"
            "\n  3. keyId in .env does not match the key pair Apple holds, or the"
            "\n     API client was revoked in ads.apple.com"
        )
    elif error == "invalid_scope":
        message += f"\n\nScope must be exactly `{SCOPE}`."
    return message


def get_access_token(force_refresh: bool = False, config: dict[str, str] | None = None) -> str:
    """The one function callers need: always returns a usable access token."""
    if not force_refresh:
        cached = _read_cache()
        if cached and cached["expires_at_dt"] - REFRESH_MARGIN > _now():
            return cached["access_token"]
    return fetch_new_token(config)["access_token"]


def token_status() -> dict:
    """Cache state for humans, without touching the network."""
    cached = _read_cache()
    if not cached:
        return {"cached": False}
    remaining = cached["expires_at_dt"] - _now()
    return {
        "cached": True,
        "expires_at": cached["expires_at_dt"],
        "seconds_remaining": int(remaining.total_seconds()),
        "stale": remaining <= REFRESH_MARGIN,
        "scope": cached.get("scope", ""),
        "token_type": cached.get("token_type", ""),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch/inspect the Apple Search Ads access token.")
    parser.add_argument("--print", dest="emit", action="store_true",
                        help="write the raw access token to stdout (a credential)")
    parser.add_argument("--force", action="store_true", help="ignore the cache and fetch a new token")
    parser.add_argument("--clear", action="store_true", help="delete the cached token and exit")
    parser.add_argument("--status", action="store_true", help="report cache state without any network call")
    args = parser.parse_args()

    if args.clear:
        print("Cache cleared." if clear_cache() else "No cache to clear.")
        return 0

    if args.status:
        status = token_status()
        if not status["cached"]:
            print("No cached token.")
        else:
            state = "STALE (will refresh on next use)" if status["stale"] else "fresh"
            print(f"Cached token: {state}")
            print(f"  expires at       : {status['expires_at']:%Y-%m-%d %H:%M:%S %Z}")
            print(f"  seconds remaining: {status['seconds_remaining']}")
            print(f"  scope            : {status['scope']}")
        return 0

    try:
        token = get_access_token(force_refresh=args.force)
    except (ConfigError, TokenError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"error: could not reach {TOKEN_URL}: {exc}", file=sys.stderr)
        return 1

    if args.emit:
        print(token)
        return 0

    status = token_status()
    print("Access token ready.")
    print(f"  expires at       : {status['expires_at']:%Y-%m-%d %H:%M:%S %Z}")
    print(f"  seconds remaining: {status['seconds_remaining']}")
    print(f"  scope            : {status['scope']}")
    print(f"  cached in        : {CACHE_FILE.name} (mode 600)")
    print("\nNot printed by default. Use --print to emit the token itself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
