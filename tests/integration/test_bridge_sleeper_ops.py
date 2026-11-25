"""Remote sleeper management carried over the bridge (#86, stage 2).

Two kinds of harness, both in-process:

- a **real pair**: two :class:`Bridge` objects over OS pipes, each with its own
  state root, standing in for two hosts joined by ``conjoin``;
- a **fake peer**: one bridge whose peer is a raw pipe the test writes frames
  into, with everything the bridge sends captured, for the cases where what
  matters is the exact frames and their order.

The requester's CLI is stage 3, so a request is written into the spool here
the way that CLI will write it (``docs/protocol.md``, the request file).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any, cast

import pytest

from spanreed import bridge, sleeper_ops
from spanreed.bridge import Bridge
from spanreed.sleeper_remote import RequestRecord, Worker
from spanreed.store import StateStore, pid_start_time

FEATURES_HELLO = {"kind": "hello", "host": "adolin", "features": ["sleeper-ops"]}


def _wait(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.02)
    raise AssertionError("condition not met within timeout")


def _stamp(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def spool_request(
    store: StateStore,
    peer: str,
    *,
    op: str = "list",
    args: dict[str, Any] | None = None,
    expires_in: float = 30,
    suffix: str = ".json",
    request_id: str | None = None,
) -> str:
    """Write a request into ``sleeper-ops/out/<peer>/`` as the stage 3 CLI will."""
    request_id = request_id or uuid.uuid4().hex
    now = datetime.now(UTC)
    spool = store.root / "sleeper-ops" / "out" / peer
    spool.mkdir(parents=True, exist_ok=True)
    (spool / f"{request_id}{suffix}").write_text(
        json.dumps(
            {
                "request_id": request_id,
                "created_at": _stamp(now),
                "expires_at": (now + timedelta(seconds=expires_in)).isoformat(),
                "op": op,
                "args": args if args is not None else {},
                "requested_by": "jmonk on test",
            }
        )
    )
    return request_id


def spool_file(store: StateStore, peer: str, request_id: str, suffix: str) -> Path:
    return store.root / "sleeper-ops" / "out" / peer / f"{request_id}{suffix}"


def reply(store: StateStore, peer: str, request_id: str) -> dict[str, Any] | None:
    path = spool_file(store, peer, request_id, ".reply.json")
    return json.loads(path.read_text()) if path.exists() else None


def audit(store: StateStore) -> list[dict[str, Any]]:
    path = store.root / "logs" / "sleeper-ops.jsonl"
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


@pytest.fixture
def tools(tmp_path: Path) -> str:
    """A PATH holding fake ``claude`` and ``spanreed``, for a remote create."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("claude", "spanreed"):
        exe = bin_dir / tool
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return str(bin_dir)


# ------------------------------------------------------------------ harnesses


class Pair:
    """Two hosts' bridges over OS pipes: A (``hostA``) dials, B (``hostB``) serves."""

    def __init__(self, tmp_path: Path) -> None:
        self.store_a = StateStore(root=tmp_path / "a")
        self.store_b = StateStore(root=tmp_path / "b")

    def link(self) -> tuple[Bridge, Bridge]:
        a2b_r, a2b_w = os.pipe()
        b2a_r, b2a_w = os.pipe()
        kw: dict[str, Any] = {"poll_interval": 0.02, "sync_interval": 30, "recv_timeout": 60}
        a = Bridge(
            os.fdopen(b2a_r, "rb"),
            os.fdopen(a2b_w, "wb"),
            "hostA",
            self.store_a,
            role="connect",
            **kw,
        )
        b = Bridge(
            os.fdopen(a2b_r, "rb"),
            os.fdopen(b2a_w, "wb"),
            "hostB",
            self.store_b,
            role="serve",
            **kw,
        )
        return a, b


@contextlib.contextmanager
def running(*bridges: Bridge) -> Generator[list[threading.Thread], None, None]:
    threads = [threading.Thread(target=b.run, daemon=True) for b in bridges]
    for t in threads:
        t.start()
    try:
        yield threads
    finally:
        for b in bridges:
            b.stop()
        for t in threads:
            t.join(timeout=10)


class FakePeer:
    """One bridge, ``kaladin``, whose peer ``adolin`` is a pipe this test drives."""

    def __init__(self, store: StateStore, *, worker: Worker | None = None) -> None:
        self.store = store
        peer_r, peer_w = os.pipe()
        self.sink = io.BytesIO()
        self.bridge = Bridge(
            os.fdopen(peer_r, "rb"),
            self.sink,
            "kaladin",
            store,
            role="serve",
            poll_interval=0.02,
            sync_interval=30,
            recv_timeout=60,
            sleeper_worker=worker,
        )
        self.peer: IO[bytes] = os.fdopen(peer_w, "wb")
        self.thread = threading.Thread(target=self.bridge.run, daemon=True)

    def send(self, frame: dict[str, Any]) -> None:
        self.peer.write((json.dumps(frame) + "\n").encode())
        self.peer.flush()

    def frames(self, kind: str | None = None) -> list[dict[str, Any]]:
        lines = self.sink.getvalue().decode().splitlines()
        every = [json.loads(x) for x in lines if x.strip()]
        return [f for f in every if kind is None or f.get("kind") == kind]

    def start(self, hello: dict[str, Any] | None = None) -> FakePeer:
        self.thread.start()
        self.send(hello if hello is not None else FEATURES_HELLO)
        _wait(lambda: self.store.read_peer_link("adolin") is not None)
        return self

    def end(self) -> None:
        """Close the pipe (EOF, as when ssh drops) and wait for run() to return."""
        with contextlib.suppress(OSError):
            self.peer.close()
        self.thread.join(timeout=10)
        assert not self.thread.is_alive()


@contextlib.contextmanager
def fake_peer(
    store: StateStore, hello: dict[str, Any] | None = None, **kw: Any
) -> Generator[FakePeer, None, None]:
    peer = FakePeer(store, **kw).start(hello)
    try:
        yield peer
    finally:
        peer.end()


def op_frame(
    op: str = "list", args: dict[str, Any] | None = None, *, expires_in: float = 30
) -> dict[str, Any]:
    return {
        "kind": "sleeper-op",
        "request_id": uuid.uuid4().hex,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
        "op": op,
        "args": args if args is not None else {},
        "requested_by": "jmonk on adolin",
    }


# ----------------------------------------------------------- hello and features


class TestFeatures:
    def test_hello_advertises_sleeper_ops(self, store: StateStore) -> None:
        with fake_peer(store) as peer:
            hello = peer.frames()[0]
        assert hello == {"kind": "hello", "host": "kaladin", "features": ["sleeper-ops"]}

    def test_both_lists_are_recorded_on_the_peer_record(self, store: StateStore) -> None:
        with fake_peer(store):
            link = store.read_peer_link("adolin")
        assert link is not None
        assert link.bridge_features == ["sleeper-ops"]
        assert link.peer_features == ["sleeper-ops"]

    def test_a_hello_with_no_features_records_null_not_empty(self, store: StateStore) -> None:
        """Absent means "too old to say", which an empty list would misstate."""
        with fake_peer(store, {"kind": "hello", "host": "adolin"}):
            link = store.read_peer_link("adolin")
        assert link is not None
        assert link.peer_features is None
        assert link.bridge_features == ["sleeper-ops"]

    def test_malformed_features_read_as_null_and_are_noted(self, store: StateStore) -> None:
        with fake_peer(store, {"kind": "hello", "host": "adolin", "features": "sleeper-ops"}):
            _wait(lambda: (store.read_peer_link("adolin") or _no_link()).note is not None)
            link = store.read_peer_link("adolin")
        assert link is not None
        assert link.peer_features is None
        assert link.note is not None and "'features'" in link.note


def _no_link() -> Any:
    raise AssertionError("no peer record")


# ------------------------------------------------------------- the round trip


class TestRoundTrip:
    def test_a_request_crosses_and_its_answer_comes_back(self, tmp_path: Path, tools: str) -> None:
        """Spool → sleeper-op → target's worker → sleeper-op-result → reply file."""
        pair = Pair(tmp_path)
        pair.store_b.set_sleeper_remote_access(tools)
        request_id = spool_request(pair.store_a, "hostB", op="list")
        with running(*pair.link()):
            _wait(lambda: reply(pair.store_a, "hostB", request_id) is not None)
        answer = reply(pair.store_a, "hostB", request_id)
        assert answer is not None
        assert answer["outcome"] == "ok" and answer["exit"] == 0
        assert answer["records"] == []
        assert not spool_file(pair.store_a, "hostB", request_id, ".json").exists()
        assert spool_file(pair.store_a, "hostB", request_id, ".sent").exists()
        events = [(e["event"], e["peer"]) for e in audit(pair.store_b)]
        assert events == [("received", "hostA"), ("done", "hostA")]

    def test_a_request_to_a_host_with_the_switch_off_is_refused(self, tmp_path: Path) -> None:
        """Off by default: the round trip works and the answer is ``refused``, exit 4."""
        pair = Pair(tmp_path)
        request_id = spool_request(pair.store_a, "hostB", op="list")
        with running(*pair.link()):
            _wait(lambda: reply(pair.store_a, "hostB", request_id) is not None)
        answer = reply(pair.store_a, "hostB", request_id)
        assert answer is not None
        assert (answer["outcome"], answer["exit"]) == ("refused", 4)
        assert "remote-access on" in answer["stderr"]

    def test_either_end_can_ask_the_other(self, tmp_path: Path, tools: str) -> None:
        """Decision 1: the frames go both ways, so the serve end can manage the dialler."""
        pair = Pair(tmp_path)
        pair.store_a.set_sleeper_remote_access(tools)
        request_id = spool_request(pair.store_b, "hostA", op="list")
        with running(*pair.link()):
            _wait(lambda: reply(pair.store_b, "hostA", request_id) is not None)
        answer = reply(pair.store_b, "hostA", request_id)
        assert answer is not None and answer["outcome"] == "ok"

    def test_a_create_result_follows_a_fresh_registry_frame(
        self, store: StateStore, tools: str, tmp_path: Path
    ) -> None:
        """The requester mirrors the new sleeper before its CLI hears the result."""
        store.set_sleeper_remote_access(tools)
        proj = tmp_path / "proj"
        proj.mkdir()
        sent = op_frame(
            "create",
            {
                "name": "nap",
                "cwd": str(proj),
                "permission_mode": "plan",
                "model": None,
                "idle_timeout": None,
                "trust": False,
                "claude_args": [],
            },
        )
        with fake_peer(store) as peer:
            _wait(lambda: len(peer.frames("registry")) >= 1)  # the one at attach
            before = len(peer.frames())
            peer.send(sent)
            _wait(lambda: len(peer.frames("sleeper-op-result")) == 1)
            after = peer.frames()[before:]
        kinds = [f["kind"] for f in after]
        assert "registry" in kinds
        assert kinds.index("registry") < kinds.index("sleeper-op-result")
        registry = after[kinds.index("registry")]
        assert "agent-nap" in [a["agent_id"] for a in registry["agents"]]
        assert after[kinds.index("sleeper-op-result")]["outcome"] == "ok"


# --------------------------------------------------- the target's thread model


class TestOnTheTarget:
    def test_the_reader_keeps_reading_while_an_operation_runs(
        self, store: StateStore, tools: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An operation runs on the worker; the reader goes on delivering frames."""
        store.set_sleeper_remote_access(tools)
        release = threading.Event()

        def blocked_list(_store: StateStore, out: Callable[[str], None]) -> int:
            assert release.wait(10)
            out("listed")
            return 0

        monkeypatch.setattr(sleeper_ops, "list_sleepers", blocked_list)
        with fake_peer(store) as peer:
            peer.send(op_frame("list"))
            peer.send(
                {
                    "kind": "msg",
                    "message": {
                        "msg_id": "m-1",
                        "from_agent": "agent-x@adolin",
                        "to_agent": "agent-here",
                        "body": "hi",
                        "ts": "2026-01-01T00:00:00Z",
                        "in_reply_to": None,
                    },
                }
            )
            _wait(lambda: len(store.recv_messages("agent-here")) == 1)
            assert peer.frames("sleeper-op-result") == []
            release.set()
            _wait(lambda: len(peer.frames("sleeper-op-result")) == 1)

    def test_operations_run_one_at_a_time_in_arrival_order(
        self, store: StateStore, tools: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R6: a second request waits behind the first instead of running beside it."""
        store.set_sleeper_remote_access(tools)
        trace: list[str] = []

        def slow_wake(_store: StateStore, name: str, out: Callable[[str], None]) -> int:
            trace.append(f"start {name}")
            time.sleep(0.2)
            trace.append(f"end {name}")
            out(f"woke {name}")
            return 0

        monkeypatch.setattr(sleeper_ops, "wake", slow_wake)
        with fake_peer(store) as peer:
            peer.send(op_frame("wake", {"name": "one"}))
            peer.send(op_frame("wake", {"name": "two"}))
            _wait(lambda: len(peer.frames("sleeper-op-result")) == 2)
        assert trace == ["start one", "end one", "start two", "end two"]

    def test_a_request_another_live_process_holds_is_answered_when_done(
        self, store: StateStore, tools: str
    ) -> None:
        """A resend of a request another live bridge is running is not run here.

        It gets nothing until the record turns ``done``, then the stored result.
        """
        store.set_sleeper_remote_access(tools)
        sent = op_frame("list")
        holder = os.getppid()
        record = RequestRecord(
            request=sent,
            received_at=datetime.now(UTC),
            bridge_pid=holder,
            bridge_pid_start=pid_start_time(holder),
            state="running",
        )
        records = store.root / "sleeper-ops" / "in" / "adolin"
        records.mkdir(parents=True)
        path = records / f"{sent['request_id']}.json"
        path.write_text(record.model_dump_json())
        with fake_peer(store) as peer:
            peer.send(sent)
            time.sleep(0.3)
            assert peer.frames("sleeper-op-result") == []
            result: dict[str, Any] = {
                "kind": "sleeper-op-result",
                "request_id": sent["request_id"],
                "outcome": "ok",
                "exit": 0,
                "stdout": "from the other bridge",
                "stderr": "",
                "records": [],
            }
            path.write_text(
                record.model_copy(update={"state": "done", "result": result}).model_dump_json()
            )
            _wait(lambda: len(peer.frames("sleeper-op-result")) == 1)
            assert peer.frames("sleeper-op-result") == [result]

    def test_a_stopping_bridge_lets_the_operation_in_flight_finish(
        self, store: StateStore, tools: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The serve end's process ends with its link; the result is stored first."""
        store.set_sleeper_remote_access(tools)
        started = threading.Event()

        def slow_wake(_store: StateStore, name: str, out: Callable[[str], None]) -> int:
            started.set()
            time.sleep(0.5)
            out(f"woke {name}")
            return 0

        monkeypatch.setattr(sleeper_ops, "wake", slow_wake)
        sent = op_frame("wake", {"name": "one"})
        peer = FakePeer(store).start()
        peer.send(sent)
        assert started.wait(5)
        peer.end()  # EOF mid-operation; run() returns only after the worker
        path = store.root / "sleeper-ops" / "in" / "adolin" / f"{sent['request_id']}.json"
        record = RequestRecord.model_validate_json(path.read_text())
        assert record.state == "done"
        assert record.result is not None and record.result["outcome"] == "ok"

    def test_a_sleeper_op_before_the_hello_is_noted_not_run(self, store: StateStore) -> None:
        peer = FakePeer(store)
        peer.thread.start()
        peer.send(op_frame("list"))
        peer.send(FEATURES_HELLO)
        _wait(lambda: store.read_peer_link("adolin") is not None)
        link = store.read_peer_link("adolin")
        peer.end()
        assert link is not None and link.note is not None
        assert "'sleeper-op' frame arrived BEFORE the peer's hello" in link.note
        assert audit(store) == []

    def test_a_result_before_the_hello_is_noted_not_written(self, store: StateStore) -> None:
        request_id = spool_request(store, "adolin", suffix=".sent")
        peer = FakePeer(store)
        peer.thread.start()
        peer.send({"kind": "sleeper-op-result", "request_id": request_id, "outcome": "ok"})
        peer.send(FEATURES_HELLO)
        _wait(lambda: store.read_peer_link("adolin") is not None)
        link = store.read_peer_link("adolin")
        peer.end()
        assert link is not None and link.note is not None
        assert "'sleeper-op-result' frame arrived BEFORE the peer's hello" in link.note
        assert reply(store, "adolin", request_id) is None


# ------------------------------------------------------ the requester's spool


class TestRequesterSpool:
    def test_a_peer_without_the_feature_is_sent_nothing(self, store: StateStore) -> None:
        """An older peer would drop the frame silently, so the request is held, and noted."""
        request_id = spool_request(store, "adolin")
        with fake_peer(store, {"kind": "hello", "host": "adolin"}) as peer:
            _wait(lambda: request_id in ((store.read_peer_link("adolin") or _no_link()).note or ""))
            time.sleep(0.2)  # several more loop turns
            assert peer.frames("sleeper-op") == []
        assert spool_file(store, "adolin", request_id, ".json").exists()
        link = store.read_peer_link("adolin")
        assert link is not None and link.note is not None
        assert "NOT sent" in link.note and "predates" in link.note

    def test_a_held_sent_request_is_noted_as_already_sent(self, store: StateStore) -> None:
        """A ``.sent`` request held on attach went out over an earlier link, so it
        is not said to "expire unsent"; the note says it is not resent."""
        unsent = spool_request(store, "adolin")
        in_flight = spool_request(store, "adolin", suffix=".sent")
        with fake_peer(store, {"kind": "hello", "host": "adolin"}) as peer:
            _wait(lambda: in_flight in ((store.read_peer_link("adolin") or _no_link()).note or ""))
            assert peer.frames("sleeper-op") == []
        link = store.read_peer_link("adolin")
        assert link is not None and link.note is not None
        [about_unsent] = [s for s in link.note.split(". ") if unsent in s]
        [about_sent] = [s for s in link.note.split(". ") if in_flight in s]
        assert "NOT sent" in about_unsent and "expire unsent" in about_unsent
        assert "NOT resent" in about_sent and "expire unsent" not in about_sent
        assert "outcome is unknown" in link.note

    def test_a_peer_listing_other_features_is_sent_nothing(self, store: StateStore) -> None:
        request_id = spool_request(store, "adolin")
        hello = {"kind": "hello", "host": "adolin", "features": ["something-else"]}
        with fake_peer(store, hello) as peer:
            _wait(lambda: request_id in ((store.read_peer_link("adolin") or _no_link()).note or ""))
            assert peer.frames("sleeper-op") == []
        assert spool_file(store, "adolin", request_id, ".json").exists()

    def test_a_request_is_sent_once_and_marked_sent(self, store: StateStore) -> None:
        request_id = spool_request(store, "adolin", op="wake", args={"name": "x"})
        with fake_peer(store) as peer:
            _wait(lambda: len(peer.frames("sleeper-op")) == 1)
            time.sleep(0.2)
            sent = peer.frames("sleeper-op")
        assert len(sent) == 1
        frame = sent[0]
        assert frame["request_id"] == request_id
        assert (frame["op"], frame["args"], frame["requested_by"]) == (
            "wake",
            {"name": "x"},
            "jmonk on test",
        )
        assert "created_at" not in frame
        assert spool_file(store, "adolin", request_id, ".sent").exists()
        assert not spool_file(store, "adolin", request_id, ".json").exists()

    def test_the_spool_drains_on_reattach(self, store: StateStore) -> None:
        """A request in flight when the link dropped is resent; one written meanwhile is sent."""
        in_flight = spool_request(store, "adolin")
        worker = Worker()  # conjoin's: one for the process, across links
        with fake_peer(store, worker=worker) as first:
            _wait(lambda: len(first.frames("sleeper-op")) == 1)
        assert spool_file(store, "adolin", in_flight, ".sent").exists()
        written_while_down = spool_request(store, "adolin")
        with fake_peer(store, worker=worker) as second:
            _wait(lambda: len(second.frames("sleeper-op")) == 2)
            time.sleep(0.2)
            ids = [f["request_id"] for f in second.frames("sleeper-op")]
        worker.close()
        assert sorted(ids) == sorted([in_flight, written_while_down])

    def test_a_request_that_expires_in_the_spool_is_never_sent(self, store: StateStore) -> None:
        """Expired before it was sent: never sent; abandoned once the CLI's grace is over."""
        lapsed = spool_request(store, "adolin", expires_in=-5)
        long_gone = spool_request(store, "adolin", expires_in=-120)
        with fake_peer(store) as peer:
            _wait(lambda: spool_file(store, "adolin", long_gone, ".abandoned").exists())
            time.sleep(0.2)
            assert peer.frames("sleeper-op") == []
        # Within the 60 s grace, left for a living CLI to abandon itself.
        assert spool_file(store, "adolin", lapsed, ".json").exists()

    def test_a_sent_request_that_expired_is_not_resent_on_reattach(self, store: StateStore) -> None:
        lapsed = spool_request(store, "adolin", expires_in=-5, suffix=".sent")
        long_gone = spool_request(store, "adolin", expires_in=-120, suffix=".sent")
        with fake_peer(store) as peer:
            _wait(lambda: spool_file(store, "adolin", long_gone, ".abandoned").exists())
            time.sleep(0.2)
            assert peer.frames("sleeper-op") == []
        assert spool_file(store, "adolin", lapsed, ".sent").exists()

    def test_a_result_is_written_only_for_a_request_that_was_sent(self, store: StateStore) -> None:
        # Written but not sent (held: the peer is too old), so the spool exists
        # and only the missing .sent stands between this result and a reply.
        unasked = spool_request(store, "adolin")
        result = {
            "kind": "sleeper-op-result",
            "request_id": unasked,
            "outcome": "ok",
            "exit": 0,
            "stdout": "",
            "stderr": "",
            "records": None,
        }

        def note() -> str:
            return (store.read_peer_link("adolin") or _no_link()).note or ""

        with fake_peer(store, {"kind": "hello", "host": "adolin"}) as peer:
            # The note saying it is held comes first; it would overwrite ours.
            _wait(lambda: "NOT sent" in note())
            peer.send(result)
            _wait(lambda: f"no {unasked}.sent" in note())
            assert reply(store, "adolin", unasked) is None
            peer.send({**result, "request_id": "../../escape"})
            _wait(lambda: "escape" in note())
        assert "invalid request_id" in note()
        assert not list(store.root.rglob("*.reply.json"))


# --------------------------------------------------- a lost result never re-runs


def test_a_lost_result_is_answered_from_the_record_without_running_again(
    tmp_path: Path, tools: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R8 and the idempotency record, end to end over two links.

    The target runs the request, and its answer is lost with the link. The
    requester resends on reattach; the target answers from its record and the
    operation does not run a second time.
    """
    pair = Pair(tmp_path)
    pair.store_b.set_sleeper_remote_access(tools)
    runs: list[str] = []

    def counted_wake(_store: StateStore, name: str, out: Callable[[str], None]) -> int:
        runs.append(name)
        out(f"sleeper {name}: launched")
        return 0

    monkeypatch.setattr(sleeper_ops, "wake", counted_wake)
    request_id = spool_request(pair.store_a, "hostB", op="wake", args={"name": "nap"})

    a, b = pair.link()
    lost: list[dict[str, Any]] = []
    real_send = b._send  # pyright: ignore[reportPrivateUsage]

    def losing_send(frame: dict[str, Any]) -> bool:
        if frame.get("kind") == "sleeper-op-result":
            lost.append(frame)
            return True  # "sent", and never arrives
        return real_send(frame)

    monkeypatch.setattr(b, "_send", losing_send)
    with running(a, b):
        _wait(lambda: len(lost) == 1)
    assert runs == ["nap"]
    assert reply(pair.store_a, "hostB", request_id) is None
    assert spool_file(pair.store_a, "hostB", request_id, ".sent").exists()

    with running(*pair.link()):
        _wait(lambda: reply(pair.store_a, "hostB", request_id) is not None)
    answer = reply(pair.store_a, "hostB", request_id)
    assert answer is not None and answer["outcome"] == "ok"
    assert answer["stdout"] == "sleeper nap: launched"
    assert runs == ["nap"], "the resend ran the operation again"
    assert [e["event"] for e in audit(pair.store_b)] == ["received", "done", "duplicate"]


# ---------------------------------------------------------- process start


class TestProcessStart:
    @pytest.fixture
    def old_record(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """An idempotency record past retention, in the default state root."""
        root = tmp_path / "state"
        monkeypatch.setenv("SPANREED_STATE_ROOT", str(root))
        sent = op_frame("list")
        record = RequestRecord(
            request=sent,
            received_at=datetime.now(UTC) - timedelta(days=8),
            bridge_pid=1,
            bridge_pid_start=None,
            state="done",
            result={"kind": "sleeper-op-result"},
        )
        path = root / "sleeper-ops" / "in" / "adolin" / f"{sent['request_id']}.json"
        path.parent.mkdir(parents=True)
        path.write_text(record.model_dump_json())
        return path

    def test_the_serve_end_prunes_old_records_at_start(
        self, old_record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[bool] = []

        class Recorder:
            def __init__(self, *_a: object, **_k: object) -> None:
                seen.append(old_record.exists())

            def run(self) -> float:
                return 0.0

        monkeypatch.setattr(bridge, "Bridge", Recorder)
        bridge.serve(self_host="adolin")
        assert seen == [False], "pruned before the bridge was built"

    def test_conjoin_prunes_at_start_and_shares_one_worker_across_links(
        self, old_record: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workers: list[object] = []

        def fake_once(*_a: object, **kwargs: object) -> float:
            workers.append(kwargs["sleeper_worker"])
            return 0.0

        def fake_loop(argv: list[str], label: str, store: StateStore, **loop_kw: object) -> int:
            assert not old_record.exists(), "pruned before the first link"
            run_once = cast("Callable[..., float]", loop_kw["run_once"])
            for _ in range(2):
                run_once(None, label, store, [None])
            return 0

        monkeypatch.setattr(bridge, "reconnect_loop", fake_loop)
        monkeypatch.setattr(bridge, "_run_bridge_once", fake_once)
        bridge.connect("adolin", self_host="kaladin", remote_spanreed="/r/spanreed")
        assert len(workers) == 2 and workers[0] is workers[1]
        assert isinstance(workers[0], Worker)
