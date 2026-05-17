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
import os
import secrets
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel

from spanreed.protocol import Agent, Message


class _RegistryDoc(BaseModel):
    """On-disk shape of ``registry.json``. Internal — callers use the StateStore API."""

    agents: list[Agent]


STALE_TTL = timedelta(hours=1)
"""Agents whose ``last_seen`` is older than this are treated as stale even if
their PID is still alive. Backstop against PID reuse for very long-lived
listeners that never call ``touch_agent``."""

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


def is_stale(agent: Agent, now: datetime, ttl: timedelta = STALE_TTL) -> bool:
    """True if the agent should be treated as no longer present on the bus."""
    if not _is_pid_alive(agent.pid):
        return True
    return now - agent.last_seen > ttl


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
        replaced (upsert) — used by the plugin so the SessionStart hook and the
        Monitor can coordinate on a deterministic id without needing to persist
        it between them. ``focus`` is preserved across upserts (session restarts
        don't wipe what the agent set last time). If ``agent_id`` is omitted, a
        fresh random one is generated.
        """
        agent = Agent(
            agent_id=agent_id if agent_id is not None else _new_id("agent"),
            name=name,
            working_dir=working_dir,
            pid=pid,
            last_seen=datetime.now(UTC),
        )
        with self._registry_lock():
            agents = self._read_registry_unlocked()
            for i, existing in enumerate(agents):
                if existing.agent_id == agent.agent_id:
                    # Preserve focus across re-registration — describes
                    # what the agent is working on, not session state.
                    agent.focus = existing.focus
                    agents[i] = agent
                    break
            else:
                agents.append(agent)
            self._write_registry_unlocked(agents)
        return agent

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
        now = datetime.now(UTC)
        return [a for a in agents if not is_stale(a, now)]

    def prune_stale(self) -> int:
        """Permanently remove stale agents from the registry. Returns count removed."""
        now = datetime.now(UTC)
        with self._registry_lock():
            agents = self._read_registry_unlocked()
            live = [a for a in agents if not is_stale(a, now)]
            removed = len(agents) - len(live)
            if removed:
                self._write_registry_unlocked(live)
        return removed

    def touch_agent(self, agent_id: str) -> None:
        """Update ``last_seen`` for an agent. No-op if not registered."""
        now = datetime.now(UTC)
        with self._registry_lock():
            agents = self._read_registry_unlocked()
            changed = False
            for agent in agents:
                if agent.agent_id == agent_id:
                    agent.last_seen = now
                    changed = True
                    break
            if changed:
                self._write_registry_unlocked(agents)

    # ------------------------------------------------------------------ inboxes

    def _inbox_path(self, agent_id: str) -> Path:
        return self._inboxes_dir / f"{agent_id}.jsonl"

    def send_message(
        self,
        from_agent: str,
        to_agent: str,
        body: str,
        in_reply_to: str | None = None,
    ) -> Message:
        """Append a message to the recipient's inbox and return it."""
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

    # ------------------------------------------------------------------ blocking wait

    def wait_for_reply(
        self,
        agent_id: str,
        in_reply_to: str,
        timeout_s: float,
        *,
        poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    ) -> Message | None:
        """Block until a message replying to ``in_reply_to`` lands in the agent's inbox.

        Returns the reply, or ``None`` on timeout. Only considers messages that
        arrive *after* this call starts — pre-existing matching messages are
        ignored. Polls at ``poll_interval_s`` (default 100ms).
        """
        deadline = time.monotonic() + timeout_s
        existing = self.recv_messages(agent_id)
        seen_count = len(existing)
        while True:
            messages = self.recv_messages(agent_id)
            for msg in messages[seen_count:]:
                if msg.in_reply_to == in_reply_to:
                    return msg
            seen_count = len(messages)
            if time.monotonic() >= deadline:
                return None
            time.sleep(poll_interval_s)
