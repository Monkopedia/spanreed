"""Sleeper records: Claude Code sessions spanreed manages inside tmux.

A sleeper sleeps when idle and wakes on mail (``docs/architecture.md``,
"Sleepers"). This module holds only the on-disk record and its lock. The record
at ``<state>/sleepers/<name>.json`` is the source of truth for a sleeper's
presence on the bus: an asleep sleeper has no process, so its registry row's pid
proves nothing about it.

It deliberately imports nothing from :mod:`spanreed.store`, which consults these
records for liveness and so imports this module.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
from collections.abc import Generator
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from spanreed._fsmode import private_dir, touch_private, write_private_text
from spanreed.protocol import SleeperState

PERMISSION_MODES = ("acceptEdits", "auto", "bypassPermissions", "dontAsk", "manual", "plan")
"""The ``--permission-mode`` choices ``claude --help`` lists (Claude Code 2.1.283).

Checked at create because the mode is passed on every launch for the sleeper's
whole life: a typo accepted here would make every wake fail, long after the
person who typed it has gone.
"""

DEFAULT_IDLE_TIMEOUT_S = 900

_DURATION_RE = re.compile(r"(\d+)([smh]?)")
_DURATION_UNITS = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_duration(text: str) -> int:
    """Seconds in ``text``: ``900``, ``900s``, ``15m`` or ``1h``. Raises ``ValueError``."""
    match = _DURATION_RE.fullmatch(text.strip())
    if match is None or int(match.group(1)) <= 0:
        raise ValueError(
            f"{text!r} is not a duration: use a positive whole number of seconds, "
            f"optionally with s, m or h (e.g. 900, 15m, 1h)"
        )
    return int(match.group(1)) * _DURATION_UNITS[match.group(2)]


class SleeperError(RuntimeError):
    """A sleeper command refused, with the reason a human should read."""


def sleeper_agent_id(name: str) -> str:
    """The registry id of sleeper ``name``: ``agent-<name>``, as for any named agent."""
    return f"agent-{name}"


class InboxPosition(BaseModel):
    """A point in an inbox: every line up to and including ``line`` (1-based).

    ``line`` 0 is "before the first line". ``msg_id`` is the id on line ``line``,
    so a check can tell that the inbox was rewritten underneath the position.
    """

    line: int = 0
    msg_id: str | None = None


class WakeFailure(BaseModel):
    """Why the last wake did not bring the agent back."""

    at: datetime
    reason: str


class SkippedLine(BaseModel):
    """An inbox line the poison bound gave up on. Kept for good, never overwritten."""

    line: int
    msg_id: str | None
    at: datetime
    why: str


INBOX_MONITOR_COMMAND = "spanreed inbox-watch"
"""The plugin's inbox Monitor, as ``Stop.background_tasks`` names it. Always running
in a sleeper, so the one background task that never keeps it awake."""


class Activity(BaseModel):
    """``<name>.activity``: the last turn boundary the sleeper's hooks reported.

    Written by ``spanreed sleeper _activity`` from the plugin's ``UserPromptSubmit``
    and ``Stop`` hooks; read by the runner to decide idleness.
    """

    event: str
    """``UserPromptSubmit`` (a turn started) or ``Stop`` (a turn ended)."""
    at: datetime
    busy: list[str] = []
    """At a ``Stop``: running background work other than the inbox Monitor, from
    the hook's ``background_tasks``. Anything here keeps the idle timeout from
    firing. As of the last ``Stop``, so it errs toward busy."""


class SleeperRecord(BaseModel):
    """``<state>/sleepers/<name>.json``. The field table in ``docs/architecture.md``."""

    name: str
    agent_id: str
    cwd: str
    session_id: str
    """Chosen at create: ``--session-id`` on the first launch, ``--resume`` after."""
    permission_mode: str
    """Fixed at create and passed on every launch (#85 decision 2: "always keep
    it"). Required: with no flag, each launch would take whatever Claude Code's
    settings said at that moment, so the mode would not be kept."""
    model: str | None = None
    """Fixed at create and passed as ``--model`` on every launch, like the
    permission mode. ``None`` passes no flag: Claude Code's settings decide."""
    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S
    claude_path: str
    spanreed_path: str
    path_env: str
    created_at: datetime

    state: SleeperState = "asleep"
    generation: int = 0
    tmux_session: str | None = None
    runner_pid: int | None = None
    runner_pid_start: int | None = None
    claude_pid: int | None = None
    claude_pid_start: int | None = None
    read_through: InboxPosition = InboxPosition()
    sleep_requested: bool = False
    last_wake_failure: WakeFailure | None = None
    undelivered_line: int | None = None
    undelivered_attempts: int = 0
    skipped_lines: list[SkippedLine] = []


class SleeperDir:
    """The ``sleepers/`` directory: one record and one lock file per sleeper."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _record_path(self, name: str) -> Path:
        return self.path / f"{name}.json"

    @contextlib.contextmanager
    def lock(self, name: str) -> Generator[None, None, None]:
        """Exclusive ``fcntl.flock`` on ``<name>.lock``, the sleeper's own lock.

        The lock file is never deleted, not even by retire: a process blocked on
        the old inode would otherwise go on to act on a sleeper that a new
        ``create`` had locked through a new file.
        """
        private_dir(self.path)
        lock_path = self.path / f"{name}.lock"
        touch_private(lock_path)
        fd = os.open(lock_path, os.O_RDONLY)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def read(self, name: str) -> SleeperRecord | None:
        try:
            text = self._record_path(name).read_text()
        except FileNotFoundError:
            return None
        return SleeperRecord.model_validate_json(text)

    def write(self, record: SleeperRecord) -> None:
        """Replace the record atomically. Callers hold :meth:`lock`."""
        private_dir(self.path)
        target = self._record_path(record.name)
        tmp = target.with_suffix(".json.tmp")
        write_private_text(tmp, record.model_dump_json(indent=2))
        tmp.replace(target)

    def delete(self, name: str) -> None:
        self._record_path(name).unlink(missing_ok=True)

    def write_activity(self, name: str, activity: Activity) -> None:
        """Replace ``<name>.activity`` atomically. Hooks write it without the lock:
        a reader sees the previous activity or this one, never a torn file."""
        private_dir(self.path)
        target = self.path / f"{name}.activity"
        tmp = self.path / f"{name}.activity.{os.getpid()}.tmp"
        write_private_text(tmp, activity.model_dump_json())
        tmp.replace(target)

    def read_activity(self, name: str) -> Activity | None:
        try:
            return Activity.model_validate_json((self.path / f"{name}.activity").read_text())
        except (FileNotFoundError, ValueError):
            return None

    def names(self) -> list[str]:
        """Every sleeper with a record, sorted. Cheap: a directory listing, no parsing."""
        if not self.path.is_dir():
            return []
        return sorted(p.stem for p in self.path.glob("*.json"))

    def records(self) -> list[SleeperRecord]:
        return [r for r in (self.read(n) for n in self.names()) if r is not None]
