"""The requester's side of managing sleepers over ``conjoin`` (#86): the spool.

The CLI never touches the pipe. It drops a request file in
``sleeper-ops/out/<peer>/`` and waits for a reply file; the bridge's main
thread sends what it finds there and its reader writes the answer. The file's
**name is its state**, and every state change is a ``rename``, so the race
between a CLI giving up and the bridge sending resolves after the fact
(``docs/architecture.md``, "How a CLI call reaches the bridge: a request
spool"; the request file's fields are in ``docs/protocol.md``)::

    <request_id>.json        waiting to be sent
    <request_id>.sent        sent (or being sent); resent on reconnect until it expires
    <request_id>.abandoned   given up on, by the CLI or at expiry; never sent again
    <request_id>.reply.json  the target's answer

Nothing here writes to the pipe; :class:`Spool` hands the bridge the frames to
send and takes the results it reads.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from spanreed._fsmode import write_private_text
from spanreed.store import StateStore

ABANDON_GRACE = timedelta(seconds=60)
"""How long past ``expires_at`` the bridge leaves a request for a living CLI to
abandon itself, so that CLI's exit code stays exact ("Every request expires")."""

CLEANUP_AGE = timedelta(days=1)
"""``.abandoned`` files and orphaned replies older than this are removed (R5)."""

_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}")

_FRAME_FIELDS = ("request_id", "expires_at", "op", "args", "requested_by")
"""What a request file carries into its ``sleeper-op`` frame. ``created_at``
stays behind: the target has no use for it."""


@dataclass(frozen=True)
class Scan:
    """One pass over the spool: frames to send, and what to put on the peer record."""

    frames: list[dict[str, Any]]
    notes: list[str]


def _expires_at(request: dict[str, Any]) -> datetime | None:
    raw = request.get("expires_at")
    if not isinstance(raw, str):
        return None
    try:
        at = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return at if at.utcoffset() == timedelta(0) else None


class Spool:
    """``sleeper-ops/out/<peer>/`` on this host, as one bridge to ``peer`` sees it.

    ``peer`` is the label the peer gave in ``hello``, already validated by the
    bridge, so it is safe as a directory name. Two bridge processes for one
    peer share the directory; a rename only one of them can win decides which
    sends a request.
    """

    def __init__(self, store: StateStore, peer: str) -> None:
        self.peer = peer
        self.dir = store.root / "sleeper-ops" / "out" / peer

    def _path(self, request_id: str, suffix: str) -> Path:
        return self.dir / f"{request_id}{suffix}"

    def _requests(self, suffix: str) -> list[tuple[str, Path]]:
        """``(request_id, path)`` for every ``<id><suffix>`` file, oldest first."""
        if not self.dir.is_dir():
            return []
        found: list[tuple[float, str, Path]] = []
        for path in self.dir.iterdir():
            name = path.name
            if not name.endswith(suffix) or name.endswith(".reply.json"):
                continue
            request_id = name[: -len(suffix)]
            if not _REQUEST_ID_RE.fullmatch(request_id):
                continue  # a temp file, or not ours; never sent
            with contextlib.suppress(OSError):
                found.append((path.stat().st_mtime, request_id, path))
        return [(rid, path) for _mtime, rid, path in sorted(found)]

    def ids(self, suffix: str) -> list[str]:
        """The request id of every ``<id><suffix>`` file, oldest first."""
        return [request_id for request_id, _path in self._requests(suffix)]

    def _abandon(self, request_id: str, suffix: str) -> bool:
        try:
            os.rename(self._path(request_id, suffix), self._path(request_id, ".abandoned"))
        except FileNotFoundError:
            return False  # the CLI got there first
        return True

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return value if isinstance(value, dict) else None  # pyright: ignore[reportUnknownVariableType]

    def _frame(self, request: dict[str, Any]) -> dict[str, Any]:
        return {"kind": "sleeper-op", **{key: request.get(key) for key in _FRAME_FIELDS}}

    def _unreadable(self, request_id: str, suffix: str, why: str) -> str | None:
        """Abandon a request file the bridge can't judge the expiry of. Never sent."""
        if not self._abandon(request_id, suffix):
            return None
        return (
            f"sleeper request {request_id} in {self.dir} {why}, so it was renamed "
            f"{request_id}.abandoned and NOT sent: without a readable expires_at the bridge "
            f"cannot tell whether it is still wanted. It was written by something other "
            f"than a spanreed CLI of this version."
        )

    def take_new(self, now: datetime | None = None) -> Scan:
        """Claim every unexpired ``.json`` request (rename to ``.sent``) and return its frame.

        The rename comes first, so a request is sent at most once from here and
        never after its CLI has abandoned it. A request past its ``expires_at``
        is never sent; it is left for its CLI, then abandoned (:meth:`expire`).
        """
        now = now or datetime.now(UTC)
        frames: list[dict[str, Any]] = []
        notes: list[str] = []
        for request_id, path in self._requests(".json"):
            request = self._read(path)
            if request is None:
                if path.exists():
                    note = self._unreadable(request_id, ".json", "is not a JSON object")
                    if note:
                        notes.append(note)
                continue
            expires_at = _expires_at(request)
            if expires_at is None or request.get("request_id") != request_id:
                note = self._unreadable(
                    request_id,
                    ".json",
                    "has no UTC expires_at, or a request_id that differs from its file name",
                )
                if note:
                    notes.append(note)
                continue
            if now > expires_at:
                continue
            try:
                os.rename(path, self._path(request_id, ".sent"))
            except FileNotFoundError:
                continue  # its CLI abandoned it a moment ago: nothing was sent
            frames.append(self._frame(request))
        return Scan(frames, notes)

    def resend(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """The frame of every ``.sent`` request that has not expired, to send again.

        Run when a link attaches: a request sent over a link that then dropped
        may never have arrived. The target answers a resend from its
        idempotency record, so this never runs anything twice.
        """
        now = now or datetime.now(UTC)
        frames: list[dict[str, Any]] = []
        for request_id, path in self._requests(".sent"):
            request = self._read(path)
            if request is None or request.get("request_id") != request_id:
                continue
            expires_at = _expires_at(request)
            if expires_at is None or now > expires_at:
                continue
            frames.append(self._frame(request))
        return frames

    def expire(self, now: datetime | None = None) -> list[str]:
        """Abandon every ``.json`` or ``.sent`` more than :data:`ABANDON_GRACE` past its deadline.

        This is what bounds a request whose CLI was killed: it lives no longer
        than its timeout plus the grace, whether or not anyone is waiting.
        Returns the ids abandoned here.
        """
        now = now or datetime.now(UTC)
        abandoned: list[str] = []
        for suffix in (".json", ".sent"):
            for request_id, path in self._requests(suffix):
                request = self._read(path)
                expires_at = _expires_at(request) if request is not None else None
                if expires_at is None or now <= expires_at + ABANDON_GRACE:
                    continue  # unreadable ones are take_new's to report
                if self._abandon(request_id, suffix):
                    abandoned.append(request_id)
        return abandoned

    def clean(self, now: datetime | None = None) -> int:
        """Remove ``.abandoned`` files and replies older than :data:`CLEANUP_AGE`. Returns how many."""
        if not self.dir.is_dir():
            return 0
        cutoff = (now or datetime.now(UTC)) - CLEANUP_AGE
        removed = 0
        for path in self.dir.iterdir():
            if not (path.name.endswith(".abandoned") or path.name.endswith(".reply.json")):
                continue
            with contextlib.suppress(OSError):
                if datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff:
                    path.unlink()
                    removed += 1
        return removed

    def accept_result(self, frame: dict[str, Any]) -> str | None:
        """Write a ``sleeper-op-result`` as ``<request_id>.reply.json``, if it was asked for.

        Only a result whose ``<request_id>.sent`` exists is written. Returns
        ``None`` when written, otherwise the note for the peer record: the
        result is dropped.
        """
        request_id = frame.get("request_id")
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            return (
                f"a sleeper-op-result from {self.peer} had an invalid request_id "
                f"{str(request_id)[:80]!r} (it must be 32 lowercase hex characters) and was "
                f"dropped."
            )
        if not self._path(request_id, ".sent").exists():
            return (
                f"a sleeper-op-result from {self.peer} for request {request_id} was dropped: "
                f"there is no {request_id}.sent in {self.dir}. Either its CLI gave up and "
                f"abandoned it, or it was answered already, or this host never sent it."
            )
        tmp = self._path(request_id, f".{os.getpid()}.{threading.get_ident()}.tmp")
        write_private_text(tmp, json.dumps(frame))
        tmp.replace(self._path(request_id, ".reply.json"))
        return None
