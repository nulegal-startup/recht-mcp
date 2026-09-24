# SPDX-License-Identifier: AGPL-3.0-only
"""`recht-mcp` — run the server locally against the public API.

    recht-mcp                      # stdio, for Claude Desktop, Claude Code, …
    recht-mcp --http               # Streamable HTTP on http://127.0.0.1:8765/mcp
    recht-mcp --http --port 9000   # another port
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

from . import __version__
from .http_backend import DEFAULT_BASE_URL, HTTPBackend
from .transports import serve_http, serve_stdio


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="recht-mcp",
        description="MCP server for German federal and Land statutes and case law, "
                    "backed by the public nu:legal API.")
    p.add_argument("--http", action="store_true",
                   help="serve Streamable HTTP instead of stdio")
    p.add_argument("--host", default="127.0.0.1",
                   help="HTTP bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    p.add_argument("--path", default="/mcp", help="HTTP endpoint path (default: /mcp)")
    p.add_argument("--base-url", default=os.environ.get("RECHT_MCP_BASE_URL", DEFAULT_BASE_URL),
                   help=f"API to read from (default: {DEFAULT_BASE_URL}, "
                        "or $RECHT_MCP_BASE_URL)")
    p.add_argument("--timeout", type=float, default=30.0,
                   help="seconds per API request (default: 30)")
    p.add_argument("--log-level", default="WARNING",
                   choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p.add_argument("--version", action="version", version=f"recht-mcp {__version__}")
    args = p.parse_args(argv)

    # stderr only: on stdio, stdout is the protocol channel.
    logging.basicConfig(stream=sys.stderr, level=args.log_level,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    backend = HTTPBackend(args.base_url, timeout=args.timeout)
    if args.http:
        serve_http(backend, args.host, args.port, args.path)
    else:
        serve_stdio(backend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
