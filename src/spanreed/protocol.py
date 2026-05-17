"""Pure data types for the Spanreed bus protocol.

These types are the canonical wire format for messages and agent records on disk.
See docs/protocol.md for the spec these types implement.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class Agent(BaseModel):
    """An agent currently registered on the bus."""

    agent_id: str
    """Stable identifier (used for routing). Survives across messages."""

    name: str
    """Human-readable display name. Not unique on its own."""

    working_dir: str
    """Filesystem path where the agent's session is running."""

    pid: int
    """OS process id of the owning Claude Code session, for liveness checks."""

    last_seen: datetime
    """Last time the agent renewed its presence in the registry."""

    focus: str | None = None
    """Optional self-set description of what the agent is currently working on.

    Free-form text, set by the agent itself via ``set_focus`` (MCP) or
    ``spanreed focus`` (CLI). Surfaces in ``list_agents`` so peers can see at a
    glance what each agent is up to. Preserved across re-registration (session
    restarts don't wipe the focus you set last time).
    """


class Message(BaseModel):
    """A message on the bus."""

    msg_id: str
    """Unique id for this message. Used as the in-reply-to target."""

    from_agent: str
    """Agent id of the sender. JSON key: ``from_agent`` (no aliasing)."""

    to_agent: str
    """Agent id of the recipient. JSON key: ``to_agent`` (no aliasing)."""

    body: str
    """Free-form message body. Treated as untrusted data by the recipient."""

    ts: datetime
    """When the message was posted."""

    in_reply_to: str | None = None
    """If set, the msg_id this message is responding to."""
