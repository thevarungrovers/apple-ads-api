"""pytest wiring. The test files also run as plain scripts; this adds nothing they need.

Points the log database at a throwaway directory for the whole run, so a test
that forgets `use_temp_db()` writes to a temp file rather than to the operator's
real `logs/apple-ads.db` -- the only record of real-money mutations.
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

from apple_ads_mcp import logdb  # noqa: E402

_SESSION_DIR = tempfile.TemporaryDirectory()
logdb.LOG_DIR = pathlib.Path(_SESSION_DIR.name) / "logs"
logdb.DB_PATH = logdb.LOG_DIR / "apple-ads.db"
logdb._local = threading.local()
logdb._session_id = None
