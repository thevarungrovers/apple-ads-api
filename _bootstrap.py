"""Re-exec the current script under venv/ when it was launched with a bare `python3`.

The dependencies (PyJWT, cryptography, requests, python-dotenv) live in this
directory's venv, so `python3 test_connection.py` would otherwise die with
`ModuleNotFoundError: No module named 'cryptography'`. Rather than make everyone
remember the ./venv/bin/python prefix, the entry-point scripts call ensure_venv()
before importing anything third-party.

Stdlib only -- this module has to work in the interpreter that is missing the deps.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
VENV_PYTHON = HERE / "venv" / "bin" / "python"
REQUIRED_MODULES = ("jwt", "cryptography", "requests", "dotenv")
REENTRY_GUARD = "APPLE_ADS_VENV_REEXEC"


def in_local_venv() -> bool:
    """True when sys.executable already belongs to this directory's venv.

    Compares sys.prefix, not the resolved executable path: venv/bin/python is a
    symlink to the system interpreter, so resolving it would make every check
    look like a match and defeat the re-exec entirely.
    """
    try:
        return pathlib.Path(sys.prefix).resolve() == (HERE / "venv").resolve()
    except OSError:
        return False


def ensure_venv() -> None:
    if in_local_venv() or os.environ.get(REENTRY_GUARD):
        return

    # Only touch the interpreter for scripts launched from THIS directory. If
    # these modules are imported as a library from another project, leave the
    # caller's interpreter alone -- hijacking someone else's process would be a
    # much worse surprise than an ImportError.
    entry = sys.argv[0] if sys.argv else ""
    if not entry or pathlib.Path(entry).resolve().parent != HERE:
        return

    if all(importlib.util.find_spec(name) is not None for name in REQUIRED_MODULES):
        return  # deps are already importable; nothing to fix

    if not VENV_PYTHON.exists():
        print(
            f"error: dependencies are missing and there is no venv at {VENV_PYTHON}.\n"
            "  Create it with:\n"
            "    python3 -m venv venv\n"
            "    ./venv/bin/pip install -r requirements.txt",
            file=sys.stderr,
        )
        raise SystemExit(1)

    os.environ[REENTRY_GUARD] = "1"  # a failed re-exec must not loop
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(pathlib.Path(entry).resolve()), *sys.argv[1:]])
