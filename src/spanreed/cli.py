"""``spanreed`` CLI for ops and debugging.

Intentionally separate from the MCP server. Useful for inspecting the bus
state from a shell without going through Claude:

- ``spanreed list``          — show currently registered agents
- ``spanreed inbox <agent>`` — dump an agent's inbox
- ``spanreed send <to> <body>`` — post a message (for manual testing)
"""

from __future__ import annotations


def main() -> None:
    """Entry point for the ``spanreed`` console script."""
    raise NotImplementedError("CLI not yet implemented")


if __name__ == "__main__":
    main()
