"""Filesystem-backed state for the Spanreed bus.

State lives under ``~/.claude/spanreed/`` by default:

- ``registry.json``      — current agents (rewritten atomically on every change).
- ``inboxes/<id>.jsonl`` — per-agent append-only message log.
- ``cursors/<id>``       — per-session "last-seen msg_id" markers.
- ``peers/<host>.json``  — one record per cross-host bridge (see :class:`PeerLink`).

Concurrency model: all registry mutations take an exclusive ``fcntl`` lock on
``registry.lock``. Inbox appends are single small JSON lines; POSIX guarantees
atomicity for writes under ``PIPE_BUF``, which is well above our line size.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import secrets
import time
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel

from spanreed.protocol import ActivityRecord, Agent, Message, PeerLink, Status


class _RegistryDoc(BaseModel):
    """On-disk shape of ``registry.json``. Internal — callers use the StateStore API."""

    agents: list[Agent]


_DEFAULT_POLL_INTERVAL_S = 0.1


def default_state_root() -> Path:
    """Where state lives by default. Override via ``SPANREED_STATE_ROOT`` env var."""
    override = os.environ.get("SPANREED_STATE_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "spanreed"


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4)}"


def _is_pid_alive(pid: int) -> bool:
    """True iff a process with the given PID currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but owned by another user.
        return True
    return True


def pid_start_time(pid: int) -> int | None:
    """Start-time of ``pid`` (Linux: clock ticks since boot), or ``None``.

    Read from field 22 of ``/proc/<pid>/stat``. Used to distinguish a live
    agent from an unrelated process that recycled its PID after the agent
    died. Returns ``None`` when unavailable — the process is already gone, or
    there's no ``/proc`` (e.g. macOS) — and callers then fall back to a bare
    PID-alive check.
    """
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm (field 2) is parenthesized and may itself contain spaces or
    # parens, so split after the FINAL ')'. The remainder begins at field 3
    # (state), making starttime (field 22) index 19 of the split.
    try:
        after = data[data.rindex(")") + 2 :].split()
        return int(after[19])
    except (ValueError, IndexError):
        return None


def is_stale(agent: Agent) -> bool:
    """True if the agent should be treated as no longer present on the bus.

    Liveness is PID-based: an agent is present iff its recorded PID is alive
    AND that PID's start-time still matches what was captured at registration.
    The start-time check guards against PID reuse — without it, an unrelated
    process that recycled a dead agent's PID would read as alive forever.

    There is deliberately NO activity/last_seen TTL: agents don't heartbeat on
    a timer (wasteful wakeups), so a live-but-quiet agent must not be flagged
    stale merely for not having sent anything recently.

    When the recorded start-time is ``None`` (couldn't be read at register, as
    on macOS), we trust the bare PID-alive check and accept the small reuse
    risk.
    """
    if not _is_pid_alive(agent.pid):
        return True
    if agent.pid_start is None:
        return False
    return pid_start_time(agent.pid) != agent.pid_start


def format_age(ts: datetime | None, *, now: datetime | None = None) -> str:
    """Render ``ts`` as a human age, e.g. ``"4s ago"`` / ``"1h 3m ago"``.

    ``None`` renders as the word ``never`` rather than an empty string, because
    every caller of this is writing a diagnosis a human will read once, and
    "never" is the answer that matters most in all of them.
    """
    if ts is None:
        return "never"
    reference = now or datetime.now(UTC)
    seconds = int((reference - ts).total_seconds())
    if seconds < 0:
        return f"{ts.isoformat()} (in the future — check the clocks on both hosts)"
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s ago"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m ago"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h ago"


_HOST_FILENAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
"""Host labels that are safe to use as a ``peers/<host>.json`` filename.

Deliberately the same shape the bridge validates a peer's self-declared label
against (``bridge._is_valid_host``), minus the length cap, and enforced a second
time here because this module turns the label into a **path**. A label with a
``/`` or a leading ``.`` would escape the peers directory; this module must not
depend on another module having checked first.
"""


def peer_link_is_attached(link: PeerLink) -> bool:
    """True iff ``link``'s bridge process is still running.

    The same PID + start-time test :func:`is_stale` applies to agents, and for
    the same reason: a mirrored ``@host`` entry is live exactly while the bridge
    that created it is, so the link's liveness must be computed identically or
    ``list`` and ``list_agents`` would disagree with each other.

    ``detached_at`` is checked first because a clean teardown is authoritative,
    but it is **not** sufficient on its own: a bridge killed with SIGKILL never
    writes it, so a record with ``detached_at is None`` may still belong to a
    long-dead process. Never read ``detached_at is None`` as "attached".
    """
    if link.detached_at is not None:
        return False
    if not _is_pid_alive(link.bridge_pid):
        return False
    if link.bridge_pid_start is None:
        return True
    return pid_start_time(link.bridge_pid) == link.bridge_pid_start


class StateStore:
    """Filesystem-backed state store for the Spanreed bus."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_state_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._inboxes_dir = self.root / "inboxes"
        self._cursors_dir = self.root / "cursors"
        self._peers_dir = self.root / "peers"
        self._inboxes_dir.mkdir(exist_ok=True)
        self._cursors_dir.mkdir(exist_ok=True)
        self._peers_dir.mkdir(exist_ok=True)
        self._registry_path = self.root / "registry.json"
        self._registry_lock_path = self.root / "registry.lock"
        self._config_path = self.root / "config.json"
        self._activity_log_path = self.root / "activity-log.jsonl"

    # ------------------------------------------------------------------ registry

    @contextlib.contextmanager
    def _registry_lock(self) -> Generator[None, None, None]:
        """Exclusive advisory lock on the registry (fcntl-based, single-host)."""
        self._registry_lock_path.touch(exist_ok=True)
        fd = os.open(self._registry_lock_path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read_registry_unlocked(self) -> list[Agent]:
        if not self._registry_path.exists():
            return []
        return _RegistryDoc.model_validate_json(self._registry_path.read_text()).agents

    def _write_registry_unlocked(self, agents: list[Agent]) -> None:
        tmp = self._registry_path.with_suffix(".json.tmp")
        tmp.write_text(_RegistryDoc(agents=agents).model_dump_json(indent=2))
        tmp.replace(self._registry_path)

    def register_agent(
        self,
        name: str,
        working_dir: str,
        pid: int,
        agent_id: str | None = None,
    ) -> Agent:
        """Insert or update an agent in the registry. Returns the (refreshed) Agent.

        If ``agent_id`` is supplied and already present, the existing entry is
        refreshed (upsert): ``pid``/``pid_start``/``last_seen`` are updated and
        ``status`` is **reset to ``None``**. Name, working_dir, and focus are
        preserved across re-registration — the SessionStart hook fires on every
        restart and would otherwise wipe any manual rename / focus the agent set
        last session. ``status`` is the exception: a fresh session isn't
        ``blocked`` just because the last one was, so a stale status is reset
        rather than carried. If ``agent_id`` is omitted, a fresh random one is
        generated and a new entry is added.
        """
        new_entry = Agent(
            agent_id=agent_id if agent_id is not None else _new_id("agent"),
            name=name,
            working_dir=working_dir,
            pid=pid,
            pid_start=pid_start_time(pid),
            last_seen=datetime.now(UTC),
        )
        with self._registry_lock():
            agents = self._read_registry_unlocked()
            for i, existing in enumerate(agents):
                if existing.agent_id == new_entry.agent_id:
                    # Refresh session-state fields; preserve name/working_dir/
                    # focus (agent customizations). Reset status — a stale
                    # status from a prior session would mislead the fleet view.
                    existing.pid = new_entry.pid
                    existing.pid_start = new_entry.pid_start
                    existing.last_seen = new_entry.last_seen
                    existing.status = None
                    agents[i] = existing
                    self._write_registry_unlocked(agents)
                    return existing
            agents.append(new_entry)
            self._write_registry_unlocked(agents)
        return new_entry

    def set_name(self, agent_id: str, name: str) -> Agent | None:
        """Update an agent's display name. Returns updated Agent, or ``None`` if not registered.

        Persists across re-registration (the SessionStart hook won't overwrite it).
        """
        with self._registry_lock():
            agents = self._read_registry_unlocked()
            for agent in agents:
                if agent.agent_id == agent_id:
                    agent.name = name
                    self._write_registry_unlocked(agents)
                    return agent
            return None

    def set_focus(self, agent_id: str, focus: str | None) -> Agent | None:
        """Update an agent's focus. Returns the updated Agent, or ``None`` if not registered.

        Empty string ``focus`` is normalized to ``None`` (cleared).
        """
        normalized = focus if focus else None
        with self._registry_lock():
            agents = self._read_registry_unlocked()
            for agent in agents:
                if agent.agent_id == agent_id:
                    changed = agent.focus != normalized
                    agent.focus = normalized
                    self._write_registry_unlocked(agents)
                    if changed:
                        self._append_activity(agent, "focus", normalized)
                    return agent
            return None

    def set_status(self, agent_id: str, status: Status | None) -> Agent | None:
        """Update an agent's status. Returns the updated Agent, or ``None`` if not registered.

        Pure registry write — does NOT notify any peer (status is pull-queried
        via ``list_agents``, not pushed). Reset to ``None`` on re-registration.
        """
        with self._registry_lock():
            agents = self._read_registry_unlocked()
            for agent in agents:
                if agent.agent_id == agent_id:
                    changed = agent.status != status
                    agent.status = status
                    self._write_registry_unlocked(agents)
                    if changed:
                        self._append_activity(agent, "status", status)
                    return agent
            return None

    def deregister_agent(self, agent_id: str) -> None:
        """Remove an agent from the registry. No-op if not present."""
        with self._registry_lock():
            agents = self._read_registry_unlocked()
            kept = [a for a in agents if a.agent_id != agent_id]
            if len(kept) != len(agents):
                self._write_registry_unlocked(kept)

    def list_agents(self, *, include_stale: bool = False) -> list[Agent]:
        """Return registered agents. By default filters out stale entries."""
        with self._registry_lock():
            agents = self._read_registry_unlocked()
        if include_stale:
            return agents
        return [a for a in agents if not is_stale(a)]

    def prune_stale(self) -> int:
        """Permanently remove stale agents from the registry. Returns count removed."""
        with self._registry_lock():
            agents = self._read_registry_unlocked()
            live = [a for a in agents if not is_stale(a)]
            removed = len(agents) - len(live)
            if removed:
                self._write_registry_unlocked(live)
        return removed

    def sync_remote_agents(
        self,
        home_host: str,
        remote_agents: list[Agent],
        owner_pid: int,
        owner_pid_start: int | None,
    ) -> None:
        """Mirror a peer's agents into this registry, qualified by ``@home_host``.

        Used by the cross-host bridge. ``remote_agents`` are the peer's *bare*
        local agents; each is stored locally as ``<id>@<home_host>`` owned by
        the bridge's own pid/pid_start, so :func:`is_stale` treats them as live
        exactly while the bridge is. Replaces any prior ``@home_host`` entries
        (so departed remote agents drop out).
        """
        suffix = f"@{home_host}"
        now = datetime.now(UTC)
        mirrored = [
            Agent(
                agent_id=f"{a.agent_id}{suffix}",
                name=a.name,
                working_dir=a.working_dir,
                pid=owner_pid,
                pid_start=owner_pid_start,
                last_seen=now,
                focus=a.focus,
                status=a.status,
            )
            for a in remote_agents
        ]
        with self._registry_lock():
            kept = [a for a in self._read_registry_unlocked() if not a.agent_id.endswith(suffix)]
            self._write_registry_unlocked(kept + mirrored)

    def clear_remote_agents(self, home_host: str) -> None:
        """Remove all mirrored ``@home_host`` entries (bridge teardown)."""
        suffix = f"@{home_host}"
        with self._registry_lock():
            agents = self._read_registry_unlocked()
            kept = [a for a in agents if not a.agent_id.endswith(suffix)]
            if len(kept) != len(agents):
                self._write_registry_unlocked(kept)

    # ------------------------------------------------------------------ peers

    def _peer_path(self, host: str) -> Path:
        """Path of ``host``'s peer record. Raises on a label that isn't a filename."""
        if not _HOST_FILENAME_RE.fullmatch(host):
            raise ValueError(
                f"{host!r} is not a usable peer host label: it must start with a letter "
                "or digit and contain only letters, digits, '.', '-' or '_'. The label is "
                "used as a filename under peers/, so anything else could escape that "
                "directory."
            )
        return self._peers_dir / f"{host}.json"

    def write_peer_link(self, link: PeerLink) -> None:
        """Persist a bridge's view of one peer (atomically). Overwrites by host."""
        path = self._peer_path(link.host)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(link.model_dump_json(indent=2))
        tmp.replace(path)

    def list_peer_links(self) -> list[PeerLink]:
        """Every peer record on this bus, attached or not, sorted by host.

        Records are read by globbing the directory rather than by building a
        path from a caller-supplied host, so an untrusted ``@host`` suffix from
        a message address can never be turned into a path here. Unreadable or
        malformed files are skipped: a corrupt peer record must not take down
        ``list`` or message resolution, which is the surface that has to keep
        working while a bridge is misbehaving.
        """
        links: list[PeerLink] = []
        for path in sorted(self._peers_dir.glob("*.json")):
            try:
                links.append(PeerLink.model_validate_json(path.read_text()))
            except (OSError, ValueError):
                continue
        return sorted(links, key=lambda peer_link: peer_link.host)

    def read_peer_link(self, host: str) -> PeerLink | None:
        """The peer record for ``host``, or ``None`` if this bus has never bridged it."""
        return next((link for link in self.list_peer_links() if link.host == host), None)

    # ------------------------------------------------------------------ inboxes

    def _inbox_path(self, agent_id: str) -> Path:
        return self._inboxes_dir / f"{agent_id}.jsonl"

    def _resolve_recipient(self, to_agent: str) -> str:
        """Resolve ``to_agent`` to a registered ``agent_id``, or raise.

        A bare ``inbox.open("a")`` will happily create an inbox for any string,
        so addressing a message by display *name* (or a typo) silently lands it
        in a file no monitor tails — the message is lost with no error to the
        sender. This guards that: the recipient must be in the registry.

        Resolution, in order:

        - ``to_agent`` is a known ``agent_id`` → use it as-is, **stale included**.
          An id is canonical and unambiguous, and a session that is merely
          restarting must keep accepting mail that waits for it. (The caller is
          still told it was not delivered to a running session — see
          :meth:`delivery_verdict`.)
        - ``to_agent`` matches exactly one **live** agent's display name →
          resolve to that agent's id (``send_message("ksrpc", ...)`` works).
        - several live agents share the name → ambiguous, raise.
        - the name matches only agents whose session has **exited** → raise.
          This is the silent-loss case from issue #55: two dead rows shared a
          display name with a live agent, a send to that name resolved to a dead
          local inbox, and the sender was told it succeeded. A stopped session is
          never a name-resolution target; addressing one requires its exact id.
        - otherwise → unknown recipient, raise with a message that distinguishes
          *no such agent* from *this host has no synced registry for that host*.
          Those are different faults and the second one reads exactly like the
          first unless the error says so (issue #55).
        """
        agents = self.list_agents(include_stale=True)
        if any(a.agent_id == to_agent for a in agents):
            return to_agent
        by_name = [a for a in agents if a.name == to_agent]
        live_by_name = [a for a in by_name if not is_stale(a)]
        if len(live_by_name) == 1:
            return live_by_name[0].agent_id
        if len(live_by_name) > 1:
            ids = ", ".join(f"{a.agent_id} (pid {a.pid})" for a in live_by_name)
            raise ValueError(
                f"{to_agent!r} is a display name shared by multiple live agents "
                f"({ids}); address by agent_id. Agents whose session has exited are "
                f"not counted here, so this is a genuine ambiguity between running "
                f"sessions."
            )
        if by_name:
            raise ValueError(self._stale_name_error(to_agent, by_name))
        raise ValueError(self._unknown_recipient_error(to_agent, agents).strip())

    @staticmethod
    def _stale_name_error(to_agent: str, dead_rows: list[Agent]) -> str:
        """Why a display name matching only exited sessions is refused, not resolved."""
        rows = ", ".join(
            f"{a.agent_id} (pid {a.pid}, no longer running, last registered "
            f"{a.last_seen.isoformat()})"
            for a in dead_rows
        )
        return (
            f"{to_agent!r} is a display name, and every agent carrying it on this bus "
            f"has STOPPED: {rows}. Refusing to resolve it. Delivering here would append "
            f"to an inbox file that no monitor is tailing and report success to you, "
            f"which is how a message is lost with nobody noticing. If you meant one of "
            f"those inboxes anyway — a session that is restarting does pick its mail up "
            f"— address it by its exact agent_id, which still resolves. Otherwise call "
            f"list_agents for the agents that are actually running; a live agent may "
            f"carry a longer display name that these stale rows were shadowing."
        )

    def _unknown_recipient_error(self, to_agent: str, agents: list[Agent]) -> str:
        """Why ``to_agent`` matched nothing — host-aware.

        The pre-#55 message said only "Call list_agents to find the recipient's
        agent_id". For a ``<id>@<host>`` address whose host has never synced its
        registry, ``list_agents`` returns nothing for that host and so *confirms*
        the wrong conclusion — the agent looks gone when in fact this host has
        never been told about it. Three days were lost to that. So an address
        that names a host is diagnosed against that host's bridge record.
        """
        if "@" not in to_agent:
            return (
                f"{to_agent!r} is not a registered agent_id or display name on this "
                f"host. Call list_agents to find the recipient's agent_id (pass "
                f"include_stale=true to also see agents whose session has exited). If "
                f"you meant an agent on ANOTHER host, address it as "
                f"<agent_id>@<host>: `spanreed list` shows which hosts are bridged to "
                f"this one and when each last synced its registry."
            )
        local_part, _, host = to_agent.rpartition("@")
        link = self.read_peer_link(host) if _HOST_FILENAME_RE.fullmatch(host) else None
        prefix = (
            f"{to_agent!r} is not a registered agent_id on this host. It addresses "
            f"{local_part!r} on host {host!r}, which is reachable only through a "
            f"`spanreed conjoin` bridge. "
        )
        not_evidence = (
            f"This is NOT evidence that {local_part!r} has stopped: list_agents on this "
            f"host cannot see it either, for exactly the same reason, so an empty "
            f"list_agents here confirms nothing about {host!r}. "
        )
        if link is None:
            return (
                prefix + f"This bus has NO BRIDGE to {host!r} — no `spanreed conjoin` "
                f"has ever attached that host here, so none of its agents have ever "
                f"been addressable from this host. " + not_evidence + f"Start the "
                f"bridge with `spanreed conjoin {host}` and wait for the first registry "
                f"sync; `spanreed list` shows every peer this bus has bridged and when "
                f"each last synced."
            )
        if not peer_link_is_attached(link):
            detached = (
                f"at {link.detached_at.isoformat()}"
                if link.detached_at is not None
                else "without tearing down cleanly, so its process is simply gone"
            )
            return (
                prefix + f"The bridge to {host!r} is DETACHED: the `spanreed conjoin` "
                f"process that owned it (pid {link.bridge_pid}, role {link.role}) ended "
                f"{detached}. Mirrored agents are dropped when their bridge goes away, "
                f"so nothing on {host!r} is addressable from this host until the bridge "
                f"is restarted (`spanreed conjoin {host}`). " + not_evidence
            )
        attached_for = format_age(link.attached_at)
        if link.last_registry_at is None:
            return (
                prefix + f"The bridge to {host!r} is ATTACHED (conjoin pid "
                f"{link.bridge_pid}, role {link.role}, attached {attached_for}) but has "
                f"NEVER RECEIVED A REGISTRY SNAPSHOT from {host!r}, so this host knows "
                f"none of its agents and can address none of them. Registry sync and "
                f"message transport are independent paths: messages would still cross "
                f"this bridge in both directions, which is why everything else looks "
                f"healthy. " + not_evidence + f"We have asked {host!r} to advertise "
                f"{link.registry_requests_sent} time(s); the last frame of any kind "
                f"arrived {format_age(link.last_frame_at)}. Run `spanreed list` here "
                f"for the full peer state, and `spanreed list` ON {host!r} to check it "
                f"has live local agents to advertise."
            )
        synced = format_age(link.last_registry_at)
        if not link.last_registry_agents:
            peer_rows = (
                f" At that moment the registry on {host} held {link.peer_registry_rows} "
                f"local row(s), {link.peer_stale_rows} of which {host!r} judged stale and "
                f"therefore withheld."
                if link.peer_registry_rows is not None
                else ""
            )
            return (
                prefix + f"The bridge to {host!r} is ATTACHED and last synced {synced}, "
                f"but that snapshot advertised ZERO AGENTS.{peer_rows} The bridge is "
                f"working; {host!r} has nothing live to advertise, so the fix belongs on "
                f"{host!r}: run `spanreed list` there and check its own agents are "
                f"registered and their pids alive. " + not_evidence
            )
        known = sorted(a.agent_id for a in agents if a.agent_id.endswith(f"@{host}"))
        advertising = ", ".join(known) if known else "(none currently in the registry)"
        return (
            prefix + f"The bridge to {host!r} is ATTACHED and healthy — it synced "
            f"{link.last_registry_agents} agent(s) {synced}, over "
            f"{link.registry_syncs} sync(s) — so this is a genuinely unknown agent "
            f"rather than a sync failure. {host!r} is currently advertising: "
            f"{advertising}. Call list_agents for the full set."
        )

    def delivery_verdict(self, agent_id: str) -> tuple[bool, str]:
        """Whether a live process is watching ``agent_id``'s inbox, and why.

        Resolution succeeding does not mean anyone will read the message: an
        exact ``agent_id`` resolves even when that session has exited, because
        queuing mail for a restarting session is deliberate. The two facts must
        not be conflated in what the sender is told — reporting a queued message
        as delivered is the silent failure issue #55 calls worse than the bug it
        was reported for. Callers surface the second element verbatim.

        Returns ``(True, ...)`` only when something is actually tailing that
        inbox: a live local session, or — for a mirrored ``<id>@<host>`` inbox —
        an attached bridge that will forward it.
        """
        entry = next(
            (a for a in self.list_agents(include_stale=True) if a.agent_id == agent_id), None
        )
        if entry is None:
            return False, (
                f"NOT DELIVERED TO A LIVE SESSION: nothing is registered as {agent_id!r} "
                f"on this bus, so the message sits in inboxes/{agent_id}.jsonl with no "
                f"agent and no bridge attached to it. Nothing will ever read it unless an "
                f"agent registers under exactly that id."
            )
        _, _, host = agent_id.rpartition("@")
        if "@" in agent_id:
            link = self.read_peer_link(host) if _HOST_FILENAME_RE.fullmatch(host) else None
            if link is None or not peer_link_is_attached(link):
                return False, (
                    f"NOT DELIVERED TO A LIVE SESSION: {agent_id!r} is a mirrored entry "
                    f"for host {host!r}, and this bus has no attached bridge to {host!r} "
                    f"right now. The message was appended to inboxes/{agent_id}.jsonl and "
                    f"will be forwarded when `spanreed conjoin {host}` next attaches and "
                    f"drains that inbox — treat it as QUEUED, not delivered, and do not "
                    f"wait on a reply."
                )
            return True, (
                f"Queued for the bridge to {host!r} (conjoin pid {link.bridge_pid}, "
                f"attached {format_age(link.attached_at)}), which forwards "
                f"inboxes/{agent_id}.jsonl to {host!r} continuously. The recipient was "
                f"live in the registry snapshot {host} sent {format_age(link.last_registry_at)}; "
                f"that snapshot is the only evidence this host has of it, so delivery "
                f"beyond the bridge is not confirmed here."
            )
        if is_stale(entry):
            return False, (
                f"NOT DELIVERED TO A LIVE SESSION: {agent_id} ({entry.name}) is in the "
                f"registry but its session is no longer running (pid {entry.pid} is gone, "
                f"or was recycled onto an unrelated process; it last registered "
                f"{entry.last_seen.isoformat()}). The message was appended to "
                f"inboxes/{agent_id}.jsonl, where it waits for that agent to restart — "
                f"nothing is tailing that file right now, so treat this as QUEUED, not "
                f"delivered, and do not block waiting for a reply. Call list_agents for "
                f"the agents that are actually running."
            )
        return True, (
            f"Delivered to {agent_id} ({entry.name}), whose session is running "
            f"(pid {entry.pid}) and whose inbox monitor is tailing "
            f"inboxes/{agent_id}.jsonl."
        )

    def send_message(
        self,
        from_agent: str,
        to_agent: str,
        body: str,
        in_reply_to: str | None = None,
    ) -> Message:
        """Append a message to the recipient's inbox and return it.

        ``to_agent`` is resolved against the registry (see
        :meth:`_resolve_recipient`): an unknown recipient raises rather than
        silently creating a dead inbox, and a display name resolves to the one
        **live** agent carrying it.

        Returning normally means *appended*, which is not the same as
        *delivered*: an exact ``agent_id`` still resolves for a session that has
        exited, so the mail waits for its restart. Callers that report an
        outcome to a human or an agent must pair this with
        :meth:`delivery_verdict` and surface its explanation — the CLI and the
        MCP tool both do.
        """
        to_agent = self._resolve_recipient(to_agent)
        msg = Message(
            msg_id=_new_id("msg"),
            from_agent=from_agent,
            to_agent=to_agent,
            body=body,
            ts=datetime.now(UTC),
            in_reply_to=in_reply_to,
        )
        inbox = self._inbox_path(to_agent)
        with inbox.open("a") as f:
            f.write(msg.model_dump_json() + "\n")
        return msg

    def append_message(self, msg: Message) -> None:
        """Append a pre-built message to its recipient's inbox verbatim.

        Unlike :meth:`send_message`, this preserves the message's existing
        ``msg_id``/``ts``/``in_reply_to`` instead of minting new ones. Used by
        the cross-host bridge to deliver a message forwarded from a peer
        without breaking reply threading.

        Idempotent by ``msg_id``: a message already present in the inbox is not
        appended again, so the bridge's at-least-once resend after a reconnect
        doesn't double-deliver.
        """
        if any(existing.msg_id == msg.msg_id for existing in self.recv_messages(msg.to_agent)):
            return
        inbox = self._inbox_path(msg.to_agent)
        with inbox.open("a") as f:
            f.write(msg.model_dump_json() + "\n")

    def recv_messages(
        self,
        agent_id: str,
        since_msg_id: str | None = None,
    ) -> list[Message]:
        """Read messages from an agent's inbox.

        If ``since_msg_id`` is given, return only messages after that id. If the
        cursor isn't found in the inbox (e.g. truncated), fail safe by returning
        everything — better to deliver too much than to silently drop messages.
        """
        inbox = self._inbox_path(agent_id)
        if not inbox.exists():
            return []
        messages: list[Message] = []
        with inbox.open() as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                messages.append(Message.model_validate_json(line))
        if since_msg_id is None:
            return messages
        for i, msg in enumerate(messages):
            if msg.msg_id == since_msg_id:
                return messages[i + 1 :]
        return messages

    # ------------------------------------------------------------------ cursors

    def _cursor_path(self, session_id: str) -> Path:
        return self._cursors_dir / session_id

    def get_cursor(self, session_id: str) -> str | None:
        path = self._cursor_path(session_id)
        if not path.exists():
            return None
        content = path.read_text().strip()
        return content or None

    def set_cursor(self, session_id: str, msg_id: str) -> None:
        path = self._cursor_path(session_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(msg_id)
        tmp.replace(path)

    # ------------------------------------------------------------------ config

    def _read_config(self) -> dict[str, object]:
        if not self._config_path.exists():
            return {}
        try:
            parsed = json.loads(self._config_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        return cast("dict[str, object]", parsed) if isinstance(parsed, dict) else {}

    def get_status_tracking(self) -> bool:
        """Whether status tracking is enabled bus-wide (default off).

        Gates only whether ``session-start`` injects the status-maintenance
        instruction into agent context — ``set_status`` itself always works.
        """
        return bool(self._read_config().get("status_tracking", False))

    def set_status_tracking(self, enabled: bool) -> None:
        """Enable/disable status tracking bus-wide (persisted in config.json)."""
        self._set_config_flag("status_tracking", enabled)

    def get_activity_log(self) -> bool:
        """Whether activity logging is enabled bus-wide (default off).

        When on, ``set_focus``/``set_status`` append each transition to
        ``activity-log.jsonl``. When off, nothing is written — zero cost.
        """
        return bool(self._read_config().get("activity_log", False))

    def set_activity_log(self, enabled: bool) -> None:
        """Enable/disable activity logging bus-wide (persisted in config.json)."""
        self._set_config_flag("activity_log", enabled)

    def _set_config_flag(self, key: str, value: bool) -> None:
        config = self._read_config()
        config[key] = value
        tmp = self._config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(config, indent=2))
        tmp.replace(self._config_path)

    # ------------------------------------------------------------------ activity log

    def _append_activity(
        self, agent: Agent, kind: Literal["focus", "status"], value: str | None
    ) -> None:
        """Append a focus/status transition to the activity log, if enabled.

        No-op when activity logging is off. Called from ``set_focus``/
        ``set_status`` on a genuine change (callers skip no-op re-sets).
        """
        if not self.get_activity_log():
            return
        record = ActivityRecord(
            ts=datetime.now(UTC),
            agent_id=agent.agent_id,
            name=agent.name,
            kind=kind,
            value=value,
        )
        with self._activity_log_path.open("a") as f:
            f.write(record.model_dump_json() + "\n")

    def read_activity(
        self,
        *,
        since: datetime | None = None,
        agent: str | None = None,
    ) -> list[ActivityRecord]:
        """Read the activity log in chronological order, optionally filtered.

        ``since`` keeps records at or after that time; ``agent`` keeps records
        whose ``agent_id`` OR ``name`` matches (callers may know either).
        """
        if not self._activity_log_path.exists():
            return []
        records: list[ActivityRecord] = []
        with self._activity_log_path.open() as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                record = ActivityRecord.model_validate_json(line)
                if since is not None and record.ts < since:
                    continue
                if agent is not None and agent not in (record.agent_id, record.name):
                    continue
                records.append(record)
        return records

    # ------------------------------------------------------------------ blocking wait

    def wait_for_reply(
        self,
        agent_id: str,
        in_reply_to: str,
        timeout_s: float,
        *,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> Message | None:
        """Block until a message replying to ``in_reply_to`` is in the agent's inbox.

        Returns the matching reply, or ``None`` on timeout. Considers *all*
        messages in the inbox — pre-existing matches are returned immediately,
        not skipped. (Skipping pre-existing was a footgun: any reply landing
        between the caller's ``send_message`` and ``wait_for_reply`` was
        silently missed.) Collision risk is essentially zero — msg_ids are
        random and in_reply_to matches exactly.

        Polls at ``poll_interval_s`` (default 100ms) until a match arrives or
        the deadline expires.
        """
        deadline = time.monotonic() + timeout_s
        while True:
            for msg in self.recv_messages(agent_id):
                if msg.in_reply_to == in_reply_to:
                    return msg
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_interval_s)
