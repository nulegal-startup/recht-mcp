# Contributing

Thank you for your interest in recht-mcp.

**Issues are welcome.** If a tool gives a wrong, misleading or incomplete
answer, please open an issue with the exact tool call (name and arguments), the
answer you got, and what you expected. Wrong answers about the law — a citation
resolved to the wrong decision, a missing "not complete" warning, a date that
reads as more certain than it is — are the reports we value most.

**Pull requests from outside contributors are not accepted yet.** This code
runs in production behind the hosted endpoint, and we have not yet set up the
contribution terms that accepting outside code requires. Until then, please
describe the change you would make in an issue instead; we may implement it
ourselves.

**Security vulnerabilities** must not be reported in a public issue — see
[SECURITY.md](SECURITY.md).

## Working on the code

    uv sync
    uv run pytest -q

A few rules the code base follows:

* The package has no third-party runtime dependencies, and internal imports are
  relative, so it can be vendored unchanged.
* Every data access goes through the `Backend` protocol
  (`src/recht_mcp/backend.py`). A tool never talks to the network itself.
* A tool that cannot answer returns a result with `isError: true` and a
  `reason` from `REASONS` — never a guess, and never a transport error.
* Every source file starts with `# SPDX-License-Identifier: AGPL-3.0-only`.
