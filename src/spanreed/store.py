"""Filesystem-backed state for the Spanreed bus.

State lives under ``~/.claude/spanreed/`` by default:

- ``registry.json``      — current agents (rewritten atomically on every change).
- ``inboxes/<id>.jsonl`` — per-agent append-only message log.
- ``cursors/<id>``       — per-session "last-seen msg_id" markers.

Concurrency model: all registry mutations take an exclusive ``fcntl`` lock on
``registry.lock``. Inbox appends are single small JSON lines; POSIX guarantees
atomicity for writes under ``PIPE_BUF``, which is well above our line size.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import time
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pydantic import BaseModel

from spanreed.protocol import Agent, Message, Status


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


class StateStore:
    """Filesystem-backed state store for the Spanreed bus."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_state_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self._inboxes_dir = self.root / "inboxes"
        self._cursors_dir = self.root / "cursors"
        self._inboxes_dir.mkdir(exist_ok=True)
        self._cursors_dir.mkdir(exist_ok=True)
        self._registry_path = self.root / "registry.json"
        self._registry_lock_path = self.root / "registry.lock"
        self._config_path = self.root / "config.json"

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
                    agent.focus = normalized
                    self._write_registry_unlocked(agents)
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
                    agent.status = status
                    self._write_registry_unlocked(agents)
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

    # ------------------------------------------------------------------ inboxes

    def _inbox_path(self, agent_id: str) -> Path:
        return self._inboxes_dir / f"{agent_id}.jsonl"

    def _resolve_recipient(self, to_agent: str) -> str:
        """Resolve ``to_agent`` to a registered ``agent_id``, or raise.

        A bare ``inbox.open("a")`` will happily create an inbox for any string,
        so addressing a message by display *name* (or a typo) silently lands it
        in a file no monitor tails — the message is lost with no error to the
        sender. This guards that: the recipient must be in the registry.

        Resolution (registry includes stale entries, so a mid-restart peer still
        accepts mail that waits for it):

        - ``to_agent`` is a known ``agent_id`` → use it as-is.
        - ``to_agent`` uniquely matches one agent's display name → resolve to
          that agent's id (a convenience; ``send_message("ksrpc", ...)`` works).
        - ``to_agent`` matches several agents' names → ambiguous, raise.
        - otherwise → unknown recipient, raise.
        """
        agents = self.list_agents(include_stale=True)
        if any(a.agent_id == to_agent for a in agents):
            return to_agent
        by_name = [a for a in agents if a.name == to_agent]
        if len(by_name) == 1:
            return by_name[0].agent_id
        if len(by_name) > 1:
            ids = ", ".join(a.agent_id for a in by_name)
            raise ValueError(
                f"{to_agent!r} is a display name shared by multiple agents ({ids}); "
                "address by agent_id."
            )
        raise ValueError(
            f"{to_agent!r} is not a registered agent_id or display name. "
            "Call list_agents to find the recipient's agent_id."
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
        silently creating a dead inbox, and a display name resolves to its id.
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
        config = self._read_config()
        config["status_tracking"] = enabled
        tmp = self._config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(config, indent=2))
        tmp.replace(self._config_path)

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
