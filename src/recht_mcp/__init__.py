# SPDX-License-Identifier: AGPL-3.0-only
"""recht_mcp — the MCP tool layer of nu:legal's German law corpus.

The nine tools, their validation and their answer shaping, the JSON-RPC/MCP
message handling, and a `Backend` protocol for every data access. The hosted
endpoint at https://recht.nulegal.eu/v1/mcp runs this package; `recht-mcp`
runs it locally over the public REST API.

Internal imports in this package are relative on purpose, so the package works
unchanged when it is copied into another code base at a pinned commit.
"""
__version__ = "1.0.1"
