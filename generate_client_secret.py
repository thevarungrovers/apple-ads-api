#!/usr/bin/env python3
"""Build and sign the Apple Search Ads client-secret JWT (ES256).

The client secret is a *self-signed* JWT: we sign it locally with
private-key.pem and Apple verifies it against the public key registered at
ads.apple.com -> Account Settings -> API.

This is NOT the access token -- see get_token.py for that. Apple caps the
secret's lifetime at 180 days, and it does not renew itself: once `exp`
passes, the token endpoint starts returning a bare `invalid_client`.

The secret is never written to disk. It is cheap to re-derive from
private-key.pem plus the three .env values, so get_token.py just calls
build_client_secret() in-process whenever it needs one -- one less
credential sitting in the working directory.
"""

from __future__ import annotations

import argparse
import datetime as dt
import pathlib
import sys

from _bootstrap import ensure_venv

ensure_venv()  # re-exec under venv/ if launched with a bare `python3`

import jwt
from dotenv import dotenv_values

HERE = pathlib.Path(__file__).resolve().parent
PRIVATE_KEY = HERE / "private-key.pem"
ENV_FILE = HERE / ".env"

AUDIENCE = "https://appleid.apple.com"
ALGORITHM = "ES256"
MAX_LIFETIME = dt.timedelta(days=180)  # Apple's documented hard maximum
DEFAULT_ORG_ID = "22648740"

REQUIRED_KEYS = ("APPLE_ADS_CLIENT_ID", "APPLE_ADS_TEAM_ID", "APPLE_ADS_KEY_ID")


class ConfigError(RuntimeError):
    """Missing or malformed local configuration -- never a server-side problem."""


def load_config() -> dict[str, str]:
    """Read .env and fail loudly if any credential is missing."""
    if not ENV_FILE.exists():
        raise ConfigError(
            f"{ENV_FILE.name} not found. Copy .env.example to .env and fill in the "
            "clientId / teamId / keyId shown by ads.apple.com."
        )

    config = {k: (v or "").strip() for k, v in dotenv_values(ENV_FILE).items()}
    missing = [k for k in REQUIRED_KEYS if not config.get(k)]
    if missing:
        raise ConfigError(f"{ENV_FILE.name} has no value for: {', '.join(missing)}")
    if not config.get("APPLE_ADS_ORG_ID"):
        config["APPLE_ADS_ORG_ID"] = DEFAULT_ORG_ID
    return config


def build_client_secret(
    lifetime: dt.timedelta = MAX_LIFETIME,
    config: dict[str, str] | None = None,
) -> tuple[str, dict]:
    """Return (signed_jwt, payload). Lifetime is clamped to Apple's 180-day cap."""
    config = config or load_config()

    if not PRIVATE_KEY.exists():
        raise ConfigError(
            f"{PRIVATE_KEY.name} not found. Regenerate it with:\n"
            "  openssl ecparam -genkey -name prime256v1 -noout -out private-key.pem\n"
            "...but note that a NEW key pair also needs a new API client at "
            "ads.apple.com, since Apple holds the matching public key."
        )

    issued_at = dt.datetime.now(dt.timezone.utc)
    expires_at = issued_at + min(lifetime, MAX_LIFETIME)

    # sub is the clientId and iss is the teamId. These are usually the SAME
    # SEARCHADS.<uuid> string, so a swap here is invisible on inspection and
    # only shows up as a server-side rejection. Do not "tidy" them together.
    payload = {
        "sub": config["APPLE_ADS_CLIENT_ID"],
        "iss": config["APPLE_ADS_TEAM_ID"],
        "aud": AUDIENCE,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
    }

    secret = jwt.encode(
        payload,
        PRIVATE_KEY.read_text(),
        algorithm=ALGORITHM,
        headers={"alg": ALGORITHM, "kid": config["APPLE_ADS_KEY_ID"]},
    )
    return secret, payload


def mask(value: str, keep_start: int = 14, keep_end: int = 4) -> str:
    """Enough of a credential to recognise it, not enough to reuse it."""
    if len(value) <= keep_start + keep_end:
        return value
    return f"{value[:keep_start]}...{value[-keep_end:]}"


def describe(payload: dict) -> str:
    issued = dt.datetime.fromtimestamp(payload["iat"], dt.timezone.utc)
    expires = dt.datetime.fromtimestamp(payload["exp"], dt.timezone.utc)
    remaining = expires - dt.datetime.now(dt.timezone.utc)
    return "\n".join(
        [
            f"  sub (clientId) : {mask(payload['sub'])}",
            f"  iss (teamId)   : {mask(payload['iss'])}",
            f"  aud            : {payload['aud']}",
            f"  issued at      : {issued:%Y-%m-%d %H:%M:%S UTC}",
            f"  expires at     : {expires:%Y-%m-%d %H:%M:%S UTC}",
            f"  valid for      : {remaining.days} days",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sign the Apple Search Ads client-secret JWT.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--print",
        dest="emit",
        action="store_true",
        help="write the raw JWT to stdout (it is a credential: it will land in "
             "your scrollback and any shell history that captures it)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=MAX_LIFETIME.days,
        help=f"lifetime in days, clamped to {MAX_LIFETIME.days} (default: %(default)s)",
    )
    args = parser.parse_args()

    try:
        secret, payload = build_client_secret(dt.timedelta(days=args.days))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.emit:
        print(secret)
        return 0

    kid = jwt.get_unverified_header(secret).get("kid", "")
    print("Signed client secret (ES256)")
    print(f"  kid (keyId)    : {mask(kid, 8, 4)}")
    print(describe(payload))
    print(f"  length         : {len(secret)} chars")
    print("\nNot printed by default. Use --print to emit the JWT itself.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
