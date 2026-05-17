"""MCP server entry point for Spanreed.

Exposes the bus operations as MCP tools. Each Claude Code session loads this
server via the plugin's ``.mcp.json``; servers across sessions coordinate
through a shared :class:`StateStore` on the filesystem.

A fresh :class:`StateStore` is constructed per tool call. The construction is
cheap (just ``mkdir -p`` on a few paths), and it removes any need for global
state in this module. Tests can swap state roots via the ``SPANREED_STATE_ROOT``
environment variable.
"""

from __future__ import annotations

from anyio import to_thread
from mcp.server.fastmcp import FastMCP

from spanreed.protocol import Message
from spanreed.store import StateStore

mcp_app: FastMCP = FastMCP("spanreed")


@mcp_app.tool()
def register_agent(
    name: str,
    working_dir: str,
    pid: int,
    agent_id: str | None = None,
) -> dict[str, object]:
    """Register this session as an agent on the bus and return the assigned record.

    If ``agent_id`` is provided and already present, the existing entry is
    replaced (upsert) — same semantics as the underlying store.
    """
    return (
        StateStore()
        .register_agent(name=name, working_dir=working_dir, pid=pid, agent_id=agent_id)
        .model_dump(mode="json")
    )


@mcp_app.tool()
def deregister_agent(agent_id: str) -> dict[str, object]:
    """Remove an agent from the registry. No-op if not present."""
    StateStore().deregister_agent(agent_id)
    return {"ok": True}


@mcp_app.tool()
def list_agents(include_stale: bool = False) -> list[dict[str, object]]:
    """List agents currently registered on the bus.

    Stale entries (PID dead or last_seen past the TTL) are filtered out by
    default; pass ``include_stale=True`` to see them.
    """
    return [
        a.model_dump(mode="json") for a in StateStore().list_agents(include_stale=include_stale)
    ]


@mcp_app.tool()
def send_message(
    from_agent: str,
    to_agent: str,
    body: str,
    in_reply_to: str | None = None,
) -> dict[str, object]:
    """Append a message to ``to_agent``'s inbox and return the posted message.

    The ``in_reply_to`` field, if set, threads this message as a response to a
    prior one. ``wait_for_reply`` uses this to match replies to their requests.
    """
    return (
        StateStore()
        .send_message(
            from_agent=from_agent,
            to_agent=to_agent,
            body=body,
            in_reply_to=in_reply_to,
        )
        .model_dump(mode="json")
    )


@mcp_app.tool()
def recv_messages(
    agent_id: str,
    since_msg_id: str | None = None,
) -> list[dict[str, object]]:
    """Read messages from an agent's inbox.

    With ``since_msg_id``, returns only messages after that id. With an unknown
    cursor, returns everything (fail-safe — better than silently dropping mail).
    """
    msgs = StateStore().recv_messages(agent_id=agent_id, since_msg_id=since_msg_id)
    return [m.model_dump(mode="json") for m in msgs]


@mcp_app.tool()
async def wait_for_reply(
    agent_id: str,
    in_reply_to: str,
    timeout_s: float,
) -> dict[str, object] | None:
    """Block until a message replying to ``in_reply_to`` lands, or timeout.

    Returns the reply, or ``None`` on timeout. Only considers messages that
    arrive *after* this call starts; pre-existing matching messages are ignored.
    Runs the blocking poll on a worker thread to avoid stalling the event loop.
    """
    store = StateStore()
    msg: Message | None = await to_thread.run_sync(
        store.wait_for_reply, agent_id, in_reply_to, timeout_s
    )
    return msg.model_dump(mode="json") if msg else None


def main() -> None:
    """Entry point for the ``spanreed-mcp`` console script."""
    mcp_app.run()


if __name__ == "__main__":
    main()
