"""MCP server entry point for Spanreed.

Exposes the bus operations as MCP tools. Each plugin-loaded session starts its
own server process via the plugin's ``.mcp.json``; servers coordinate through
the shared :class:`StateStore`.

Tool surface (planned, see ``docs/protocol.md``):

- ``register_agent``    — join the bus
- ``deregister_agent``  — leave cleanly
- ``list_agents``       — discover peers
- ``send_message``      — post to a peer's inbox
- ``recv_messages``     — drain new messages from own inbox
- ``wait_for_reply``    — block until a reply (or timeout) lands
"""

from __future__ import annotations


def main() -> None:
    """Entry point for the ``spanreed-mcp`` console script."""
    raise NotImplementedError("MCP server not yet implemented")


if __name__ == "__main__":
    main()
