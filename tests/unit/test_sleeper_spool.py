"""The requester's spool and the target's worker (#86, stage 2), without a bridge."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from spanreed.sleeper_remote import Worker
from spanreed.sleeper_spool import ABANDON_GRACE, CLEANUP_AGE, Spool
from spanreed.store import StateStore

NOW = datetime(2026, 11, 25, 18, 0, tzinfo=UTC)


def write(
    spool: Spool,
    *,
    expires_at: datetime | None = None,
    suffix: str = ".json",
    request_id: str | None = None,
    body: dict[str, Any] | None = None,
) -> str:
    request_id = request_id or uuid.uuid4().hex
    spool.dir.mkdir(parents=True, exist_ok=True)
    request = body or {
        "request_id": request_id,
        "created_at": NOW.isoformat(),
        "expires_at": (expires_at or NOW + timedelta(seconds=30)).isoformat(),
        "op": "list",
        "args": {},
        "requested_by": "jmonk on kaladin",
    }
    (spool.dir / f"{request_id}{suffix}").write_text(json.dumps(request))
    return request_id


def state(spool: Spool, request_id: str) -> list[str]:
    return sorted(p.name.removeprefix(request_id) for p in spool.dir.glob(f"{request_id}*"))


class TestTakeNew:
    def test_claims_a_request_and_returns_its_frame(self, store: StateStore) -> None:
        spool = Spool(store, "adolin")
        rid = write(spool)
        scan = spool.take_new(NOW)
        assert scan.frames == [
            {
                "kind": "sleeper-op",
                "request_id": rid,
                "expires_at": (NOW + timedelta(seconds=30)).isoformat(),
                "op": "list",
                "args": {},
                "requested_by": "jmonk on kaladin",
            }
        ]
        assert state(spool, rid) == [".sent"]
        assert spool.take_new(NOW).frames == [], "a claimed request is not sent again"

    def test_an_expired_request_is_not_claimed(self, store: StateStore) -> None:
        spool = Spool(store, "adolin")
        rid = write(spool, expires_at=NOW - timedelta(seconds=1))
        assert spool.take_new(NOW).frames == []
        assert state(spool, rid) == [".json"]

    def test_an_unreadable_request_is_abandoned_and_reported(self, store: StateStore) -> None:
        spool = Spool(store, "adolin")
        garbage = uuid.uuid4().hex
        spool.dir.mkdir(parents=True)
        (spool.dir / f"{garbage}.json").write_text("not json")
        mismatched = write(spool, body={"request_id": "0" * 32, "expires_at": NOW.isoformat()})
        no_expiry = write(spool, body={"request_id": "x"})
        scan = spool.take_new(NOW)
        assert scan.frames == []
        assert len(scan.notes) == 3
        for rid in (garbage, mismatched, no_expiry):
            assert state(spool, rid) == [".abandoned"]

    def test_other_files_are_ignored(self, store: StateStore) -> None:
        spool = Spool(store, "adolin")
        spool.dir.mkdir(parents=True)
        (spool.dir / "notes.json").write_text("{}")
        (spool.dir / f"{uuid.uuid4().hex}.reply.json").write_text("{}")
        assert spool.take_new(NOW) == spool.take_new(NOW)
        assert spool.take_new(NOW).frames == [] and spool.take_new(NOW).notes == []

    def test_a_missing_spool_is_empty(self, store: StateStore) -> None:
        spool = Spool(store, "adolin")
        assert spool.take_new(NOW).frames == []
        assert spool.resend(NOW) == []
        assert spool.expire(NOW) == []
        assert spool.clean(NOW) == 0


class TestResendAndExpire:
    def test_only_unexpired_sent_requests_are_resent(self, store: StateStore) -> None:
        spool = Spool(store, "adolin")
        live = write(spool, suffix=".sent")
        write(spool, suffix=".sent", expires_at=NOW - timedelta(seconds=1))
        write(spool)  # not sent yet: take_new's, not resend's
        assert [f["request_id"] for f in spool.resend(NOW)] == [live]

    def test_requests_are_abandoned_only_after_the_grace(self, store: StateStore) -> None:
        spool = Spool(store, "adolin")
        deadline = NOW - ABANDON_GRACE
        within = write(spool, expires_at=deadline)
        past_json = write(spool, expires_at=deadline - timedelta(seconds=1))
        past_sent = write(spool, expires_at=deadline - timedelta(seconds=1), suffix=".sent")
        assert sorted(spool.expire(NOW)) == sorted([past_json, past_sent])
        assert state(spool, within) == [".json"]
        assert state(spool, past_json) == [".abandoned"]
        assert state(spool, past_sent) == [".abandoned"]


class TestClean:
    def test_old_abandoned_files_and_replies_go_and_nothing_else(self, store: StateStore) -> None:
        spool = Spool(store, "adolin")
        old = (NOW - CLEANUP_AGE - timedelta(minutes=1)).timestamp()
        young = (NOW - CLEANUP_AGE + timedelta(minutes=1)).timestamp()
        spool.dir.mkdir(parents=True)
        names = {
            "a.abandoned": old,
            "b.reply.json": old,
            "c.abandoned": young,
            "d.reply.json": young,
            "e.json": old,
            "f.sent": old,
        }
        for name, mtime in names.items():
            (spool.dir / name).write_text("{}")
            os.utime(spool.dir / name, (mtime, mtime))
        assert spool.clean(NOW) == 2
        assert sorted(p.name for p in spool.dir.iterdir()) == [
            "c.abandoned",
            "d.reply.json",
            "e.json",
            "f.sent",
        ]


class TestAcceptResult:
    def test_a_result_for_a_sent_request_becomes_an_owner_only_reply(
        self, store: StateStore
    ) -> None:
        spool = Spool(store, "adolin")
        rid = write(spool, suffix=".sent")
        result = {"kind": "sleeper-op-result", "request_id": rid, "outcome": "ok", "exit": 0}
        assert spool.accept_result(result) is None
        path = spool.dir / f"{rid}.reply.json"
        assert json.loads(path.read_text()) == result
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert state(spool, rid) == [".reply.json", ".sent"]

    def test_a_result_with_no_sent_file_is_dropped(self, store: StateStore) -> None:
        spool = Spool(store, "adolin")
        waiting = write(spool)  # not sent: a result for it is not ours to accept
        note = spool.accept_result({"kind": "sleeper-op-result", "request_id": waiting})
        assert note is not None and "no " in note and ".sent" in note
        assert state(spool, waiting) == [".json"]

    def test_a_bad_request_id_writes_nothing(self, store: StateStore) -> None:
        spool = Spool(store, "adolin")
        note = spool.accept_result({"kind": "sleeper-op-result", "request_id": "../../x"})
        assert note is not None and "invalid request_id" in note
        assert not list(store.root.rglob("*reply*"))


class TestWorker:
    class FakeTarget:
        def __init__(self, delay: float = 0.0, fail: bool = False) -> None:
            self.ran: list[str] = []
            self.delay = delay
            self.fail = fail

        def run(self, request_id: str) -> dict[str, Any] | None:
            self.ran.append(request_id)
            time.sleep(self.delay)
            if self.fail:
                raise OSError("disk full")
            return {"request_id": request_id}

    def _submit(self, worker: Worker, target: FakeTarget, rid: str, got: list[object]) -> None:
        worker.submit(target, rid, got.append)  # type: ignore[arg-type]

    def test_close_waits_for_the_one_in_flight_and_starts_no_more(self) -> None:
        worker = Worker()
        target = self.FakeTarget(delay=0.3)
        got: list[object] = []
        self._submit(worker, target, "one", got)
        deadline = time.time() + 5
        while not target.ran and time.time() < deadline:
            time.sleep(0.01)
        self._submit(worker, target, "two", got)
        assert worker.close(timeout=5)
        assert target.ran == ["one"]
        assert got == [{"request_id": "one"}]

    def test_close_reports_an_operation_that_outlasts_the_wait(self) -> None:
        worker = Worker()
        release = threading.Event()

        class Stuck:
            def run(self, _rid: str) -> None:
                release.wait(5)

        worker.submit(Stuck(), "one", lambda _r: None)  # type: ignore[arg-type]
        time.sleep(0.05)
        assert worker.close(timeout=0.1) is False
        release.set()

    def test_a_failing_run_does_not_end_the_worker(self) -> None:
        worker = Worker()
        got: list[object] = []
        self._submit(worker, self.FakeTarget(fail=True), "bad", got)
        good = self.FakeTarget()
        self._submit(worker, good, "good", got)
        deadline = time.time() + 5
        while len(got) < 2 and time.time() < deadline:
            time.sleep(0.01)
        worker.close()
        assert got == [None, {"request_id": "good"}]
