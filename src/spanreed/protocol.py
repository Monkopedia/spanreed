"""Pure data types for the Spanreed bus protocol.

These types are the canonical wire format for messages and agent records on disk.
See docs/protocol.md for the spec these types implement.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

Status = Literal["idle", "working", "needs_input", "blocked"]
"""Self-reported agent status, ordered by escalating need for a human:

- ``idle`` — registered but not actively working.
- ``working`` — actively making progress; no human needed.
- ``needs_input`` — wants a human decision/answer; may still be proceeding.
- ``blocked`` — stopped; cannot continue without a human.

The "needs a human" set is ``{needs_input, blocked}`` — a peer or dashboard
partitions on that. ``idle`` is best-effort: an agent stops running exactly when
it goes idle, so it can't always report the transition (there's no Stop hook).
"""


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

    pid_start: int | None = None
    """Start-time of ``pid`` captured at registration (Linux: clock ticks since
    boot, from ``/proc/<pid>/stat``). Used to detect PID reuse — if the agent
    dies and the OS recycles its PID onto an unrelated process, the start-time
    won't match, so liveness checks can tell the agent is actually gone.

    ``None`` when the start-time couldn't be read (e.g. no ``/proc``, as on
    macOS); liveness then falls back to a bare PID-alive check.
    """

    last_seen: datetime
    """Last time the agent renewed its presence in the registry (set on
    register/upsert). Informational only — liveness is PID-based, not
    last_seen-based, so a quiet-but-alive agent is never flagged stale."""

    focus: str | None = None
    """Optional self-set description of what the agent is currently working on.

    Free-form text, set by the agent itself via ``set_focus`` (MCP) or
    ``spanreed focus`` (CLI). Surfaces in ``list_agents`` so peers can see at a
    glance what each agent is up to. Preserved across re-registration (session
    restarts don't wipe the focus you set last time).
    """

    status: Status | None = None
    """Optional self-reported status (see :data:`Status`). Set via ``set_status``
    (MCP) or ``spanreed status`` (CLI); surfaces in ``list_agents`` so peers can
    see who needs a human, without any notification (pull, not push).

    Unlike ``focus``, this is **reset to ``None`` on re-registration** — a fresh
    session isn't ``blocked`` just because the last one was, and a stale status
    would mislead the very fleet view this exists to provide. ``None`` means
    "not reported" (status tracking off, or not yet set this session).
    """


class ActivityRecord(BaseModel):
    """One entry in the activity log — a focus or status transition.

    An append-only timeline of agent presence changes, written only when
    activity logging is enabled (off by default). Intended to be dumped (e.g.
    piped into an LLM) for a digest of what the fleet has been doing; spanreed
    emits the log, the summarization is the caller's to run.
    """

    ts: datetime
    """When the transition happened (UTC)."""

    agent_id: str
    """Agent that changed — its stable routing id."""

    name: str
    """Display name at the time of the change, so a digest can read
    "kodemirror did X" rather than a hex id."""

    kind: Literal["focus", "status"]
    """Which field changed."""

    value: str | None
    """The new value: focus text, or a :data:`Status`. ``None`` when focus was
    cleared."""


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
