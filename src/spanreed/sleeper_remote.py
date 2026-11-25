"""The target's side of managing sleepers over ``conjoin`` (#86).

A conjoined peer asks this host to run a sleeper operation with a
``sleeper-op`` frame and gets a ``sleeper-op-result`` back. This module decides
what each frame gets, and runs the operation: the switch, the expiry, the
idempotency record that keeps a resent request from running twice, the
validation of every argument, and the audit log. The design is in
``docs/architecture.md`` ("Managing a peer's sleepers over ``conjoin``") and the
frames in ``docs/protocol.md``.

The bridge owns the threads and the pipe. Its reader calls :meth:`Target.receive`
for each ``sleeper-op`` frame and does what the :class:`Receipt` says; its worker
calls :meth:`Target.run` for each queued request, one at a time. Nothing here
writes to the pipe.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from spanreed import sleeper_ops
from spanreed._fsmode import open_private, private_dir, write_private_text
from spanreed.sleepers import SleeperError
from spanreed.store import StateStore, pid_start_time, process_is_alive

MAX_LIFETIME_S = 600
"""The longest a request may live, and the cap on ``--timeout`` (R9, decision 8)."""

CLOCK_ALLOWANCE_S = 30
"""Skew allowed between the two hosts' clocks when judging ``expires_at`` (R9)."""

RECORD_RETENTION = timedelta(days=7)
"""How long an idempotency record is kept (R5, decision 6). Far longer than any
request lives, so no request the target accepts outlives the record that would
recognise it as a resend."""

OPS = ("create", "list", "wake", "sleep", "retire")

_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32}")

_ARGS: dict[str, type[BaseModel]] = {
    "create": sleeper_ops.CreateArgs,
    "list": sleeper_ops.ListArgs,
    "wake": sleeper_ops.NameArgs,
    "sleep": sleeper_ops.NameArgs,
    "retire": sleeper_ops.NameArgs,
}

Outcome = Literal["ok", "failed", "interrupted", "invalid", "expired", "refused"]

_EXIT: dict[Outcome, int] = {"interrupted": 1, "invalid": 2, "expired": 2, "refused": 4}
"""The requester's exit code for an outcome the operation itself did not produce."""


class RequestRecord(BaseModel):
    """``sleeper-ops/in/<peer>/<request_id>.json``: one per request a peer sent here."""

    request: dict[str, Any]
    """The ``sleeper-op`` frame as received, ``expires_at`` included."""
    received_at: datetime
    bridge_pid: int
    """The bridge process that holds it: queued in it, running in it, or that finished it."""
    bridge_pid_start: int | None
    state: Literal["queued", "running", "done"]
    result: dict[str, Any] | None = None
    """The ``sleeper-op-result`` frame, once ``done``."""


@dataclass(frozen=True)
class Receipt:
    """What the bridge does with a ``sleeper-op`` frame.

    - ``reply``: send ``result`` now.
    - ``run``: queue the request for the worker, which calls :meth:`Target.run`.
    - ``wait``: a live bridge process already holds it; send its result when
      :meth:`Target.result_if_done` returns one.
    - ``drop``: send nothing (there is no safe id to answer under); put
      ``note`` on the peer record.
    """

    action: Literal["reply", "run", "wait", "drop"]
    request_id: str | None = None
    result: dict[str, Any] | None = None
    note: str | None = None


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _summary(result: dict[str, Any]) -> str:
    """The first line of the operation's output."""
    text = str(result.get("stdout") or result.get("stderr") or "")
    return text.splitlines()[0] if text else ""


class _Invalid(ValueError):
    """A ``sleeper-op`` this host cannot model; the reason goes back as ``invalid``."""


def _parse(frame: dict[str, Any], now: datetime) -> tuple[str, datetime]:
    """Check every field but the id, trusting nothing the requester checked.

    Returns the op and its deadline."""
    raw_expiry = frame.get("expires_at")
    if not isinstance(raw_expiry, str):
        raise _Invalid("expires_at is missing or not a string")
    try:
        expires_at = datetime.fromisoformat(raw_expiry)
    except ValueError:
        raise _Invalid(f"expires_at {raw_expiry!r} is not an ISO 8601 timestamp") from None
    if expires_at.utcoffset() != timedelta(0):
        raise _Invalid(f"expires_at {raw_expiry!r} is not in UTC")
    if expires_at > now + timedelta(seconds=MAX_LIFETIME_S + CLOCK_ALLOWANCE_S):
        raise _Invalid(
            f"expires_at {raw_expiry!r} is more than {MAX_LIFETIME_S}s (plus a "
            f"{CLOCK_ALLOWANCE_S}s clock allowance) ahead of this host's clock; no request "
            f"may live longer"
        )
    op = frame.get("op")
    if op not in OPS:
        raise _Invalid(
            f"op {op!r} is not one this host's spanreed knows ({', '.join(OPS)}); the "
            f"requester may be newer. Upgrade spanreed here to use it"
        )
    if not isinstance(frame.get("requested_by"), str):
        raise _Invalid("requested_by is missing or not a string")
    try:
        _ARGS[op].model_validate(frame.get("args"))
    except ValidationError as exc:
        raise _Invalid(f"args for {op!r} are malformed: {exc}") from None
    return op, expires_at


class Target:
    """This host, answering one peer's sleeper requests, for one bridge process.

    ``peer`` is the label the peer gave in ``hello``, already validated by the
    bridge (it becomes a directory name). It is self-asserted: it keys the
    idempotency records, because a request id is only unique on the requester,
    and it is logged so a person can tell links apart. It grants nothing; the
    switch is one per machine.
    """

    def __init__(
        self,
        store: StateStore,
        *,
        self_host: str,
        peer: str,
        role: Literal["connect", "serve"],
        bridge_pid: int | None = None,
        bridge_pid_start: int | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.self_host = self_host
        self.peer = peer
        self.role = role
        self.bridge_pid = bridge_pid if bridge_pid is not None else os.getpid()
        self.bridge_pid_start = (
            bridge_pid_start if bridge_pid is not None else pid_start_time(self.bridge_pid)
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._dir = store.root / "sleeper-ops" / "in" / peer

    # --- the reader's half ---

    def receive(self, frame: dict[str, Any]) -> Receipt:
        """Decide what a ``sleeper-op`` frame gets. Never runs the operation.

        The order: the frame's own fields; then any record of this id, which
        decides whatever ``expires_at`` or the switch now say, and runs nothing
        new; then the switch; then the expiry; then a new record. Wherever a
        request could start running, the switch is checked first
        (open-questions.md, #86).
        """
        now = self._clock()
        request_id = frame.get("request_id")
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            self._log(
                "invalid",
                request_id=request_id,
                op=frame.get("op"),
                reason="request_id is not 32 lowercase hex characters",
            )
            return Receipt(
                "drop",
                note=(
                    f"a sleeper-op frame from the peer had an invalid request_id "
                    f"{str(request_id)[:80]!r} (it must be 32 lowercase hex characters) and "
                    f"was dropped unanswered, since there is no safe name to answer under. "
                    f"The requester's CLI will time out with 'not answered'."
                ),
            )
        try:
            op, expires_at = _parse(frame, now)
        except _Invalid as exc:
            result = self._result(request_id, "invalid", stderr=f"invalid: {exc}")
            self._log_once("invalid", frame, result)
            return Receipt("reply", request_id, result)
        record = self._read(request_id)
        if record is not None:
            return self._decide(request_id, record)
        if self.store.get_sleeper_remote_access() is None:
            result = self._refused(request_id)
            self._log_once("refused", frame, result)
            return Receipt("reply", request_id, result)
        if now > expires_at:
            result = self._expired(request_id, expires_at)
            self._write_new(request_id, frame, now, "done", result)
            self._log_once("expired", frame, result)
            return Receipt("reply", request_id, result)
        if not self._write_new(request_id, frame, now, "queued"):
            # Another bridge process for this peer recorded it between our read
            # and our write. Its record decides, as for any resend.
            record = self._read(request_id)
            assert record is not None
            return self._decide(request_id, record)
        self._log(
            "received",
            request_id=request_id,
            expires_at=frame["expires_at"],
            op=op,
            args=frame["args"],
            requested_by=frame["requested_by"],
        )
        return Receipt("run", request_id)

    def _decide(self, request_id: str, record: RequestRecord) -> Receipt:
        """A request id seen before: the record decides. It never runs twice."""
        if record.state == "done":
            assert record.result is not None
            self._log(
                "duplicate",
                request_id=request_id,
                op=record.request.get("op"),
                outcome=record.result.get("outcome"),
            )
            return Receipt("reply", request_id, record.result)
        if process_is_alive(record.bridge_pid, record.bridge_pid_start):
            return Receipt("wait", request_id)
        if record.state == "running":
            result = self._result(
                request_id,
                "interrupted",
                stderr=(
                    f"interrupted: the bridge on {self.self_host} ended while running this; "
                    f"its effect may be partial. Check `spanreed sleeper list --host "
                    f"{self.self_host}`."
                ),
            )
            self._finish(request_id, record, result, "interrupted")
            return Receipt("reply", request_id, result)
        # Queued in a process that died before starting it.
        expires_at = datetime.fromisoformat(str(record.request["expires_at"]))
        if self._clock() > expires_at:
            result = self._expired(request_id, expires_at)
            self._finish(request_id, record, result, "expired")
            return Receipt("reply", request_id, result)
        if self.store.get_sleeper_remote_access() is None:
            result = self._refused(request_id)
            self._finish(request_id, record, result, "refused")
            return Receipt("reply", request_id, result)
        self._write(
            request_id,
            record.model_copy(
                update={"bridge_pid": self.bridge_pid, "bridge_pid_start": self.bridge_pid_start}
            ),
        )
        return Receipt("run", request_id)

    def result_if_done(self, request_id: str) -> dict[str, Any] | None:
        """The stored result, once the record is ``done``; for a ``wait`` receipt."""
        record = self._read(request_id)
        return record.result if record is not None and record.state == "done" else None

    # --- the worker's half ---

    def run(self, request_id: str) -> dict[str, Any]:
        """Run a queued request and return its result frame, stored in its record.

        The expiry is checked again here, since a request may wait behind
        another (R9, decision 8). So is the switch: one turned off while a
        request waited stops it before it starts (open-questions.md, #86).
        """
        record = self._read(request_id)
        assert record is not None, f"run() of {request_id}, which has no record"
        if record.state == "done":
            assert record.result is not None
            return record.result
        expires_at = datetime.fromisoformat(str(record.request["expires_at"]))
        if self._clock() > expires_at:
            result = self._expired(request_id, expires_at)
            self._finish(request_id, record, result, "expired")
            return result
        access = self.store.get_sleeper_remote_access()
        if access is None:
            result = self._refused(request_id)
            self._finish(request_id, record, result, "refused")
            return result
        self._write(request_id, record.model_copy(update={"state": "running"}))
        op = str(record.request["op"])
        result = self._execute(request_id, op, record.request["args"], access.path_env or "")
        self._finish(request_id, record, result, "done")
        return result

    def _execute(self, request_id: str, op: str, raw_args: object, path_env: str) -> dict[str, Any]:
        """Run ``op`` through the same function the local command calls."""
        args = _ARGS[op].model_validate(raw_args)
        lines: list[str] = []
        records: list[dict[str, Any]] | None = None
        try:
            if isinstance(args, sleeper_ops.CreateArgs):
                code = sleeper_ops.create(
                    self.store, args, lines.append, path_env=path_env, remote=True
                )
            elif isinstance(args, sleeper_ops.NameArgs):
                fn = {
                    "wake": sleeper_ops.wake,
                    "sleep": sleeper_ops.sleep,
                    "retire": sleeper_ops.retire,
                }[op]
                code = fn(self.store, args.name, lines.append)
            else:
                code = sleeper_ops.list_sleepers(self.store, lines.append)
                records = sleeper_ops.list_records(self.store)
            stderr = ""
        except SleeperError as exc:
            code, stderr = 1, f"spanreed sleeper {op}: {exc}"
        except Exception as exc:  # a failed operation must not take the worker down
            code, stderr = 1, f"spanreed sleeper {op}: {type(exc).__name__}: {exc}"
        return self._result(
            request_id,
            "ok" if code == 0 else "failed",
            exit_code=code,
            stdout="\n".join(lines),
            stderr=stderr,
            records=records if code == 0 else None,
        )

    # --- results ---

    def _result(
        self,
        request_id: str,
        outcome: Outcome,
        *,
        exit_code: int | None = None,
        stdout: str = "",
        stderr: str = "",
        records: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return {
            "kind": "sleeper-op-result",
            "request_id": request_id,
            "outcome": outcome,
            "exit": exit_code if exit_code is not None else _EXIT[outcome],
            "stdout": stdout,
            "stderr": stderr,
            "records": records,
        }

    def _refused(self, request_id: str) -> dict[str, Any]:
        return self._result(
            request_id,
            "refused",
            stderr=(
                f"refused: remote sleeper management is turned off on {self.self_host}.\n"
                f"  To turn it on, run this ON {self.self_host}, in a terminal:  "
                f"spanreed sleeper remote-access on"
            ),
        )

    def _expired(self, request_id: str, expires_at: datetime) -> dict[str, Any]:
        return self._result(
            request_id,
            "expired",
            stderr=(
                f"expired: its deadline ({_stamp(expires_at)}) passed before it started on "
                f"{self.self_host}, so it was not run."
            ),
        )

    # --- the idempotency record ---

    def _path(self, request_id: str) -> Path:
        return self._dir / f"{request_id}.json"

    def _read(self, request_id: str) -> RequestRecord | None:
        try:
            return RequestRecord.model_validate_json(self._path(request_id).read_text())
        except FileNotFoundError:
            return None

    def _write(self, request_id: str, record: RequestRecord) -> None:
        """Replace the record atomically."""
        tmp = self._dir / f"{request_id}.{os.getpid()}.{threading.get_ident()}.tmp"
        write_private_text(tmp, record.model_dump_json(indent=2))
        tmp.replace(self._path(request_id))

    def _write_new(
        self,
        request_id: str,
        frame: dict[str, Any],
        now: datetime,
        state: Literal["queued", "done"],
        result: dict[str, Any] | None = None,
    ) -> bool:
        """Create the record, only if none exists. ``False`` if one already did."""
        private_dir(self._dir, parents=True)
        record = RequestRecord(
            request=frame,
            received_at=now,
            bridge_pid=self.bridge_pid,
            bridge_pid_start=self.bridge_pid_start,
            state=state,
            result=result,
        )
        tmp = self._dir / f"{request_id}.{os.getpid()}.{threading.get_ident()}.tmp"
        write_private_text(tmp, record.model_dump_json(indent=2))
        try:
            os.link(tmp, self._path(request_id))  # fails if it exists: create-only, atomic
        except FileExistsError:
            return False
        finally:
            tmp.unlink()
        return True

    def _finish(
        self,
        request_id: str,
        record: RequestRecord,
        result: dict[str, Any],
        event: str,
    ) -> None:
        """Store the result (the record turns ``done``) and log its outcome."""
        self._write(
            request_id,
            record.model_copy(
                update={
                    "state": "done",
                    "result": result,
                    "bridge_pid": self.bridge_pid,
                    "bridge_pid_start": self.bridge_pid_start,
                }
            ),
        )
        self._log(
            event,
            request_id=request_id,
            op=record.request.get("op"),
            outcome=result["outcome"],
            exit=result["exit"],
            summary=_summary(result),
        )

    # --- the audit log ---

    def _log_once(self, event: str, frame: dict[str, Any], result: dict[str, Any]) -> None:
        """The one line a refused, invalid or expired-on-arrival request writes."""
        self._log(
            event,
            request_id=frame.get("request_id"),
            expires_at=frame.get("expires_at"),
            op=frame.get("op"),
            args=frame.get("args"),
            requested_by=frame.get("requested_by"),
            outcome=result["outcome"],
            exit=result["exit"],
            summary=_summary(result),
        )

    def _log(self, event: str, **fields: object) -> None:
        """Append one line to ``logs/sleeper-ops.jsonl``. Owner-only, append-only."""
        line = {
            "ts": _stamp(self._clock()),
            "event": event,
            "peer": self.peer,
            "role": self.role,
            "bridge_pid": self.bridge_pid,
            **fields,
        }
        log_dir = self.store.root / "logs"
        private_dir(log_dir, parents=True)
        with open_private(log_dir / "sleeper-ops.jsonl") as f:
            f.write(json.dumps(line, default=str) + "\n")


def prune_records(store: StateStore, now: datetime | None = None) -> int:
    """Delete idempotency records older than :data:`RECORD_RETENTION`. Returns how many.

    Run by a bridge at start. Age is the record's ``received_at``, or the file's
    mtime when the record can't be read.
    """
    cutoff = (now or datetime.now(UTC)) - RECORD_RETENTION
    pruned = 0
    for path in (store.root / "sleeper-ops" / "in").glob("*/*.json"):
        try:
            received = RequestRecord.model_validate_json(path.read_text()).received_at
        except (OSError, ValueError):
            try:
                received = datetime.fromtimestamp(path.stat().st_mtime, UTC)
            except OSError:
                continue
        if received < cutoff:
            with contextlib.suppress(OSError):
                path.unlink()
                pruned += 1
    return pruned
