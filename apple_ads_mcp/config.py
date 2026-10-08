"""Configuration for the MCP server: credentials, and the ad account to address.

Thin by design. `generate_client_secret.load_config()` is already the single
source of truth for parsing .env and for the ConfigError raised when something
is missing, so this module re-exports it rather than growing a second parser
that can drift from the first.

What it adds is `resolve_ad_account_id()`: the Platform API's account id is NOT
in REQUIRED_KEYS, because the script that discovers it has to be able to run
before it is set. So the requirement is enforced here instead, at the point of
use, with an error that says how to obtain the value rather than just that it is
absent.
"""

from __future__ import annotations

import pathlib
import sys

# Launched as `venv/bin/python /abs/path/apple_ads_mcp/server.py`, sys.path[0] is
# the package directory, not the repo root -- so the root modules this package
# reuses would not import. Fix it before the first one is touched.
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Importing this module runs `ensure_venv()` at root-module import time, which is
# safe in both directions: under venv/bin/python it returns on the in_local_venv()
# check, and under any other interpreter it returns because argv[0]'s parent is
# this package, not the repo root. Either way it never re-execs the server.
from generate_client_secret import (  # noqa: E402
    AD_ACCOUNT_KEY,
    ENV_FILE,
    PRIVATE_KEY,
    REQUIRED_KEYS,
    ConfigError,
    load_config,
    mask,
)

__all__ = [
    "AD_ACCOUNT_KEY",
    "ENV_FILE",
    "PRIVATE_KEY",
    "REPO_ROOT",
    "REQUIRED_KEYS",
    "ConfigError",
    "load_config",
    "mask",
    "resolve_ad_account_id",
    "private_key_pem",
]

_DISCOVERY_HINT = (
    "Run `./venv/bin/python test_platform_connection.py` -- step 5 reads it back "
    "from Apple and prints the line to paste into .env. Do not read it off the "
    "Apple Ads UI and do not reuse APPLE_ADS_ORG_ID: one org can own several ad "
    "accounts, and the ids are different fields on the same record."
)


def resolve_ad_account_id(config: dict[str, str] | None = None) -> str:
    """The adAccountId for `X-AP-Context`, or ConfigError saying how to get one.

    Deliberately does not fall back to the org id, and deliberately does not call
    Apple to work it out on the fly. Both would turn "which account am I about to
    write to?" into something the server decides silently at startup. It is a
    value a human pasted into .env after seeing it, or it is an error.
    """
    config = config if config is not None else load_config()
    value = (config.get(AD_ACCOUNT_KEY) or "").strip()
    if not value:
        raise ConfigError(f"{ENV_FILE.name} has no value for {AD_ACCOUNT_KEY}. {_DISCOVERY_HINT}")
    if not value.isdigit():
        raise ConfigError(
            f"{AD_ACCOUNT_KEY} is {value!r}, which is not a numeric id. {_DISCOVERY_HINT}"
        )
    return value


def private_key_pem() -> str:
    """The signing key, read fresh so a rotated key needs no server restart."""
    if not PRIVATE_KEY.exists():
        raise ConfigError(
            f"{PRIVATE_KEY.name} not found at {PRIVATE_KEY}. Without it nothing can "
            "be signed. Regenerating the key pair also needs a new API client at "
            "ads.apple.com, since Apple holds the matching public key."
        )
    return PRIVATE_KEY.read_text()
