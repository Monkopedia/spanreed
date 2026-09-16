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


class PeerLink(BaseModel):
    """One cross-host bridge, as seen from *this* host.

    Written by the bridge process to ``peers/<host>.json`` so that the state of
    a `spanreed conjoin` is visible to every other process on the bus — the
    CLI, the MCP server, and the recipient resolver. Without this record the
    only observable effect of a bridge is the mirrored ``@host`` registry
    entries it creates, which makes "the peer has no agents", "the peer never
    sent a registry", and "there is no bridge at all" indistinguishable. Those
    are three different faults with three different remedies (issue #55).

    The record is **kept after teardown**, with ``detached_at`` set, because "a
    bridge was here and died" is a diagnosis and deleting the file erases it.
    """

    host: str
    """The peer's self-declared host label — the ``@host`` suffix of every id it owns."""

    role: Literal["connect", "serve"]
    """Which end of the pipe this process is: ``connect`` dialled out
    (``spanreed conjoin <host>``), ``serve`` was dialled (``conjoin --serve``)."""

    bridge_pid: int
    """PID of the bridge process that owns this link. Its liveness *is* the
    link's liveness, exactly as for the mirrored registry entries."""

    bridge_pid_start: int | None = None
    """Start-time of ``bridge_pid``, same PID-reuse guard as ``Agent.pid_start``."""

    attached_at: datetime
    """When the peer's ``hello`` was accepted and routing began."""

    last_frame_at: datetime | None = None
    """When any frame (including the peer's keepalive pings) last arrived.
    Proves the pipe is carrying traffic even when no registry ever syncs."""

    last_registry_at: datetime | None = None
    """When a ``registry`` frame from the peer was last applied. ``None`` means
    **never** — the single most important field on this record, because that is
    the state in which the peer's agents are unaddressable while everything
    else looks healthy."""

    last_registry_agents: int | None = None
    """How many agents the peer advertised in its last ``registry`` frame.
    ``0`` is a real and distinct diagnosis from ``None``: the peer answered and
    said it has nothing live."""

    peer_registry_rows: int | None = None
    """How many bare local rows the peer's own registry held when it built that
    frame. ``None`` from a peer too old to report it."""

    peer_stale_rows: int | None = None
    """How many of ``peer_registry_rows`` the peer judged stale (and so withheld).
    ``peer_registry_rows > 0`` with ``last_registry_agents == 0`` says the peer's
    agents exist but are failing *its* liveness check — a fault on the peer, not
    in the bridge."""

    registry_syncs: int = 0
    """Count of ``registry`` frames applied over this link's lifetime."""

    registry_requests_sent: int = 0
    """Count of ``registry-request`` frames we have sent asking the peer to
    advertise. A large number with ``last_registry_at is None`` means the peer
    is hearing us and not answering (or is too old to know the frame)."""

    detached_at: datetime | None = None
    """When the bridge tore this link down cleanly. ``None`` on a link that is
    still up — *or* on one whose bridge was killed without running teardown, so
    do not read ``None`` as "attached": check ``bridge_pid`` liveness."""

    note: str | None = None
    """Free-form diagnosis attached by the bridge: a frame that could not be
    parsed, a registry frame that arrived before the handshake, the reason for
    a teardown. Verbose on purpose — this is the line a human pastes."""
