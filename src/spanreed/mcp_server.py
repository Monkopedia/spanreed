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

import os
import traceback
from datetime import UTC, datetime

from anyio import to_thread
from mcp.server.fastmcp import FastMCP

from spanreed.identity import derive_agent_identity
from spanreed.protocol import Message
from spanreed.store import StateStore, default_state_root


def _log(event: str, **fields: object) -> None:
    """Append a single line to ``~/.claude/spanreed/logs/spanreed-mcp.log``.

    Never raises — if logging fails (disk full, permissions, etc.), we'd rather
    keep the server running than crash for an observability reason.
    """
    try:
        log_dir = default_state_root() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).isoformat()
        parts = [f"{ts}", f"pid={os.getpid()}", f"event={event}"]
        parts.extend(f"{k}={v}" for k, v in fields.items())
        line = " ".join(parts) + "\n"
        with (log_dir / "spanreed-mcp.log").open("a") as f:
            f.write(line)
    except Exception:
        pass


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

    Stale entries (PID dead, or the PID's start-time no longer matches what was
    recorded — i.e. the agent's process is gone) are filtered out by default;
    pass ``include_stale=True`` to see them.
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


@mcp_app.tool()
def set_name(name: str) -> dict[str, object] | None:
    """Rename this session's display name on the bus.

    Useful when the default cwd-derived name (e.g. "git" when cwd is
    ``~/git``) isn't descriptive. Only changes the display name — the
    underlying ``agent_id`` stays the same, so existing message references
    keep working. Persists across session restarts.

    Returns the updated agent record, or ``None`` if this session isn't
    registered on the bus.
    """
    agent_id, _ = derive_agent_identity()
    agent = StateStore().set_name(agent_id, name)
    return agent.model_dump(mode="json") if agent else None


@mcp_app.tool()
def set_focus(focus: str | None = None) -> dict[str, object] | None:
    """Set (or clear) this session's focus on the bus.

    Pass a short description of what you're working on (e.g. "auth-refactor in
    the example-service repo"). Pass ``None`` or an empty string to clear.
    Returns the updated agent record, or ``None`` if this session isn't
    registered on the bus.

    The focus surfaces in ``list_agents`` so peers can see at a glance what
    you're up to. Preserved across session restarts.
    """
    agent_id, _ = derive_agent_identity()
    agent = StateStore().set_focus(agent_id, focus)
    return agent.model_dump(mode="json") if agent else None


FOCUS_UPDATE_REQUEST_MARKER = "[FOCUS_UPDATE_REQUEST]"
"""Body prefix that identifies a focus-update request on the bus.

The recipient's plugin policy recognizes this marker and replies with their
current focus. Used by ``request_focus_update`` below.
"""


@mcp_app.tool()
async def request_focus_update(
    agent_id: str,
    timeout_s: float = 30.0,
) -> str | None:
    """Ask another agent to update + report their current focus.

    Sends a focus-update request to ``agent_id``, blocks until they reply
    (up to ``timeout_s`` seconds), and returns the reply body — typically a
    short description of what they're working on. Returns ``None`` on timeout
    or if the recipient never responds.

    Use this when ``list_agents`` shows a peer with no focus set, or when their
    listed focus seems stale and you want a fresh report.
    """
    my_id, _ = derive_agent_identity()
    store = StateStore()
    msg = store.send_message(
        from_agent=my_id,
        to_agent=agent_id,
        body=(
            f"{FOCUS_UPDATE_REQUEST_MARKER} Please call spanreed.set_focus "
            "with what you're currently working on, then reply to this "
            "message with that focus text as the body."
        ),
    )
    reply: Message | None = await to_thread.run_sync(
        store.wait_for_reply, my_id, msg.msg_id, timeout_s
    )
    return reply.body if reply else None


def main() -> None:
    """Entry point for the ``spanreed-mcp`` console script.

    Wraps the FastMCP stdio loop with crash logging so we can tell after the
    fact whether the process died of its own accord (uncaught exception) or
    was shut down externally (Claude Code sent EOF / SIGTERM).
    """
    _log("startup")
    try:
        mcp_app.run()
    except SystemExit as e:
        _log("shutdown", reason="SystemExit", code=e.code)
        raise
    except KeyboardInterrupt:
        _log("shutdown", reason="KeyboardInterrupt")
        raise
    except BaseException as e:
        _log(
            "crash",
            type=type(e).__name__,
            message=str(e).replace("\n", "\\n")[:200],
            traceback=traceback.format_exc().replace("\n", "\\n"),
        )
        raise
    else:
        _log("shutdown", reason="clean")


if __name__ == "__main__":
    main()
