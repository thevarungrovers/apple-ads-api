"""Agent-callable Apple Ads layer: a stdio MCP server on the Apple Ads Platform API.

A package rather than a flat `mcp_server.py` at the repo root on purpose. The
root scripts call `_bootstrap.ensure_venv()`, which re-execs the process under
venv/ when it was launched with a bare `python3`. `ensure_venv()` returns early
unless `argv[0]`'s parent is the repo root, so a module living one directory
down can never re-exec itself -- whereas a flat `mcp_server.py` at the root
would `os.execv` itself mid-startup, which an MCP client, already mid-handshake
on this process's stdin, would see as the server dying.

The server therefore does NOT call ensure_venv(). Its registration in
~/.claude.json pins the absolute venv interpreter instead; see README.
"""

__version__ = "0.1.0"
