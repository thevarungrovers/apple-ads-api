"""pytest wiring. The test files also run as plain scripts; this adds nothing they need.

Points the log database at a throwaway directory for the whole run, so a test
that forgets `use_temp_db()` writes to a temp file rather than to the operator's
real `logs/apple-ads.db` -- the only record of real-money mutations.

Also points the naming identity at a throwaway `.env` holding the placeholders
`AB` / `max`, so no test ever reads the operator's real `.env`.
"""

from __future__ import annotations

import pathlib
import sys
import tempfile
import threading

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
for path in (REPO_ROOT, REPO_ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from apple_ads_mcp import logdb, naming  # noqa: E402

_SESSION_DIR = tempfile.TemporaryDirectory()
logdb.LOG_DIR = pathlib.Path(_SESSION_DIR.name) / "logs"
logdb.DB_PATH = logdb.LOG_DIR / "apple-ads.db"
logdb._local = threading.local()
logdb._session_id = None

# The naming identity is read from .env, which holds the OPERATOR's real values.
# Point it at a throwaway .env with placeholders before anything builds state.
_ENV_FILE = pathlib.Path(_SESSION_DIR.name) / ".env"
_ENV_FILE.write_text("APPLE_ADS_OWNER_INITIALS=AB\nAPPLE_ADS_AGENT_NAME=max\n")
naming.ENV_FILES = [_ENV_FILE]
