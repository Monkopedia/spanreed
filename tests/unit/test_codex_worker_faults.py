"""Fault injection for the Codex worker: what it does when things break.

Every test here induces a failure a real deployment can produce, and asserts
what the worker did about it. The order of preference behind the assertions,
from ``docs/architecture.md`` and from issue #55:

1. **A worker that wedges silently is the worst outcome.** #55 was an agent
   that looked alive in the registry and ingested nothing, and it cost a human
   three days. Nothing here may end in "still polling, nothing logged".
2. **A worker that exits loudly is acceptable** — provided the log names what
   happened, what the worker did about it, and what a reader should check.
3. **A worker that recovers is best**, and several of these do: a malformed
   frame is skipped, a vanished socket file changes nothing, mail that arrives
   mid-turn runs as the next turn.

The stub app-server and the spawned fake child both come from
``test_codex_client.py``, and the worker fixtures from ``test_codex_worker.py``.
A second harness would drift from the first and then prove something about
itself.

Assertions on log and reply text use long, specific phrases on purpose: this
repo has been bitten by a guard that matched ``all`` inside ``call``.
"""

from __future__ import annotations

import io
import os
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from spanreed import cli
from spanreed.codex_client import CodexClient
from spanreed.codex_worker import (
    AGENT_MESSAGE_DELTA,
    AUTH_TOKENS_REFRESH,
    THREAD_STATUS_CHANGED,
    CodexWorker,
    WorkerConfig,
    WorkerLog,
    WorkerLogUnwritable,
)
from spanreed.protocol import Message
from spanreed.store import StateStore
from tests.unit.test_codex_client import Handler, StubServer, child_cmd, server_encode
from tests.unit.test_codex_worker import (
    SECRET_TOKEN,
    MakeWorker,
    app_server,
    frames,
    log_of,
    mail,
    params_of,
    register_sender,
    replies,
    turn_text,
    write_auth,
)

not_root = pytest.mark.skipif(
    os.geteuid() == 0,
    reason="root ignores the permission bits these tests set, so the fault cannot be induced",
)

SpawnWorker = Callable[..., CodexWorker]


# ---------------------------------------------------------------- helpers


def queue_mail(store: StateStore, worker: CodexWorker, body: str) -> Message:
    """Put mail in a worker's inbox *before* it starts.

    ``send_message`` resolves the recipient against the registry, so the row
    has to exist first; ``start()`` then registers over the top of it, exactly
    as a restarted worker does. This is how a test can call :meth:`serve` — the
    whole lifecycle, with its exit code — and still have work waiting.
    """
    store.register_agent(
        name=worker.config.name,
        working_dir=str(worker.config.cwd),
        pid=os.getpid(),
        agent_id=worker.config.agent_id,
    )
    return mail(store, worker, body)


def raw_frame(stub: StubServer, payload: bytes, opcode: int = 0x1) -> None:
    """Put arbitrary bytes on the wire as one WebSocket frame.

    ``StubServer.send`` serialises JSON; these tests need the frames JSON
    cannot express — a non-object body, bytes that are not JSON at all, a close
    frame in the middle of a turn.
    """
    conn = stub.conn
    assert conn is not None, "the stub has no connection yet"
    conn.sendall(server_encode(payload, opcode))


def inbox_of(store: StateStore, worker: CodexWorker) -> Path:
    return store.root / "inboxes" / f"{worker.config.agent_id}.jsonl"


def registered(store: StateStore, worker: CodexWorker) -> bool:
    return any(a.agent_id == worker.config.agent_id for a in store.list_agents())


@pytest.fixture
def make_spawned_worker(
    worker_cwd: Path,
    store: StateStore,
) -> Iterator[SpawnWorker]:
    """A worker whose app-server is a real spawned child process.

    The stub server the other tests use lives in this process and cannot die,
    stall its own pipe, or fail to bind. Those three faults need a child, and
    the child is the one from ``test_codex_client.py`` driven by environment
    knobs.
    """
    workers: list[CodexWorker] = []

    def make(
        *,
        env: dict[str, str] | None = None,
        spawn_timeout: float = 20.0,
        timeout: float = 20.0,
        turn_timeout: float = 30.0,
        **config_kw: Any,
    ) -> CodexWorker:
        config_kw.setdefault("name", "reviewer")
        config = WorkerConfig(cwd=worker_cwd, **config_kw)

        def factory(on_request: Any, on_notification: Any) -> CodexClient:
            return CodexClient(
                codex_cmd=child_cmd(),
                env={"STUB_NOISE_BYTES": "0", **(env or {})},
                timeout=timeout,
                turn_timeout=turn_timeout,
                spawn_timeout=spawn_timeout,
                on_server_request=on_request,
                on_notification=on_notification,
            )

        worker = CodexWorker(
            config, store=store, client_factory=factory, log_stream=None, poll_interval=0.3
        )
        workers.append(worker)
        return worker

    yield make
    for worker in workers:
        worker.close()


# ------------------------------------------------------ process & transport


class TestProcessAndTransport:
    """The app-server is a child process on the far end of a unix socket.
    Every way that ends badly is here."""

    def test_app_server_killed_mid_turn_answers_the_sender_then_exits(
        self, make_spawned_worker: SpawnWorker, store: StateStore
    ) -> None:
        """SIGKILL between `turn/start` being accepted and the terminal event.

        Before this test the worker raised ConnectionError out of `run_turn`,
        `serve()` logged one line about the connection, and the sender was
        never told anything at all — it waited for a reply that no longer had a
        process behind it."""
        worker = make_spawned_worker(env={"STUB_DIE_AFTER": "turn"})
        register_sender(store)
        original = queue_mail(store, worker, "review this")

        assert worker.serve() == 1, "a dead transport must not look like a clean exit"

        log = log_of(worker)
        assert "THE APP-SERVER CONNECTION DROPPED MID-TURN" in log
        assert "EXITED with status -9" in log, "the log must say what became of the process"
        assert "it is NOT retried" in log
        (reply,) = replies(store)
        assert "the app-server connection dropped in the middle of your turn" in reply.body
        assert reply.in_reply_to == original.msg_id
        assert not registered(store, worker), "a dead worker must not stay addressable"

    def test_an_app_server_that_never_binds_fails_to_start_with_its_own_output(
        self, make_spawned_worker: SpawnWorker, store: StateStore
    ) -> None:
        """A server that starts, logs, and never listens. The diagnosis is in
        the output it produced before it stopped, so that output has to survive
        into the log rather than dying with the process."""
        worker = make_spawned_worker(env={"STUB_NEVER_BIND": "30"}, spawn_timeout=1.0)

        assert worker.serve() == 1

        log = log_of(worker)
        assert "FAILED TO START" in log
        assert "did not create" in log, "the log must name the socket that never appeared"
        assert "stub-child: alive, logging, and never binding a socket" in log
        assert "is still running" in log, "a hung server and a dead one are different faults"
        assert not registered(store, worker)

    def test_a_child_that_exits_zero_after_the_handshake_is_not_a_success(
        self, make_spawned_worker: SpawnWorker, store: StateStore
    ) -> None:
        """`initialize` answered, then the process leaves. A clean exit code is
        the most misleading version of this: the connection dies during
        `thread/start`, and the log has to say the server went away rather than
        implying the call was refused."""
        worker = make_spawned_worker(env={"STUB_DIE_AFTER": "handshake"})

        assert worker.serve() == 1

        log = log_of(worker)
        assert "FAILED TO START" in log
        # Which half of the exchange notices first is a race with the child's
        # exit — the write fails, or the read hits EOF — so both sentences are
        # accepted. What is not optional is that one of them appears: a bare
        # `[Errno 32] Broken pipe` names neither the peer nor the call.
        assert (
            "the app-server connection was gone before" in log
            or "server closed the connection during thread/start" in log
        )
        assert "EXITED with status 0" in log
        assert "stub-child: exiting 0 right after the handshake" in log
        assert not registered(store, worker)

    def test_a_colossal_write_during_a_turn_does_not_stall_the_worker(
        self, make_spawned_worker: SpawnWorker, store: StateStore
    ) -> None:
        """The regression that cost thirty spike runs, moved into a turn.

        The pipe holds 64KB. The child writes 1MB *after* accepting
        `turn/start` and before the terminal event, so a worker that is not
        draining continuously blocks the child inside the turn and then reports
        a turn that never finished — with no error anywhere, because a blocked
        writer is not a failure."""
        noise = 1_000_000
        worker = make_spawned_worker(env={"STUB_TURN_NOISE_BYTES": str(noise)}, turn_timeout=30.0)
        worker.start()
        register_sender(store)
        mail(store, worker, "say something while shouting")

        started = time.monotonic()
        assert worker.run(max_polls=1) == 1
        elapsed = time.monotonic() - started

        (reply,) = replies(store)
        assert reply.body == "child replied", "the turn must complete, not time out"
        assert elapsed < 20.0, f"the turn took {elapsed:.1f}s; the drain is not keeping up"
        captured = worker.client.server_log()
        assert len(captured) >= noise, (
            f"captured {len(captured)} of {noise} bytes the child wrote — a short capture is "
            f"what a pipe that stopped being read looks like"
        )

    def test_the_socket_file_vanishing_does_not_touch_a_live_connection(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """An established unix socket is a file descriptor, not a path. Unlinking
        the path cannot break a connection already made — worth pinning, because
        the opposite belief would send someone hunting a fault that is not
        there."""
        stub, worker = make_worker()
        stub.path.unlink()
        register_sender(store)
        mail(store, worker, "still there?")

        assert worker.run(max_polls=1) == 1
        (reply,) = replies(store)
        assert reply.body == "looks good to me"

    def test_a_websocket_close_mid_turn_answers_the_sender_then_exits(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """The server closes the connection politely instead of dying. Same
        obligation as the kill: the sender is owed an answer and the worker
        must not keep polling a socket that is gone."""

        def drop(stub: StubServer, msg: dict[str, Any]) -> None:
            stub.notify("turn/started", {})
            raw_frame(stub, b"", opcode=0x8)

        _stub, worker = make_worker(app_server(drop), start=False)
        register_sender(store)
        queue_mail(store, worker, "review this")

        assert worker.serve() == 1

        log = log_of(worker)
        assert "THE APP-SERVER CONNECTION DROPPED MID-TURN" in log
        assert "server sent a WebSocket close frame" in log
        (reply,) = replies(store)
        assert "the app-server connection dropped in the middle of your turn" in reply.body


# ----------------------------------------------------------------- protocol


class TestProtocolFaults:
    """Frames app-server should never send. Each is skipped and reported: one
    bad frame does not desynchronise a WebSocket stream, so killing the turn
    over it would throw away a working connection — but a skip nobody is told
    about is how a worker ends up ingesting nothing while looking alive."""

    @staticmethod
    def _inject(payload: bytes) -> Handler:
        """A turn that puts ``payload`` on the wire mid-stream, then finishes."""

        def stream(stub: StubServer, msg: dict[str, Any]) -> None:
            raw_frame(stub, payload)
            stub.notify(AGENT_MESSAGE_DELTA, {"delta": "the turn carried on"})
            stub.notify("turn/completed", {})

        return app_server(stream)

    def test_a_frame_that_is_not_json_is_skipped_and_named(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        _stub, worker = make_worker(self._inject(b"{this is not json at all"))
        register_sender(store)
        mail(store, worker, "go")

        assert worker.run(max_polls=1) == 1
        (reply,) = replies(store)
        assert reply.body == "the turn carried on", "one bad frame must not lose the turn"
        log = log_of(worker)
        assert "PROTOCOL FAULT from app-server" in log
        assert "a frame that is not JSON arrived during a turn and was skipped" in log

    @pytest.mark.parametrize("payload", [b'"just a string"', b"[1, 2, 3]", b"42"])
    def test_a_json_frame_that_is_not_an_object_is_skipped_and_named(
        self, make_worker: MakeWorker, store: StateStore, payload: bytes
    ) -> None:
        _stub, worker = make_worker(self._inject(payload))
        register_sender(store)
        mail(store, worker, "go")

        assert worker.run(max_polls=1) == 1
        assert replies(store)[0].body == "the turn carried on"
        assert "a JSON-RPC frame that is not an object" in log_of(worker)

    def test_a_response_for_an_id_we_never_sent_is_reported_not_dropped(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """Silently discarding it was the old behaviour, and it hides the one
        thing that matters: somebody else is on this socket, or the server has
        confused two clients."""
        _stub, worker = make_worker(self._inject(b'{"id": 4242, "result": {"threads": []}}'))
        register_sender(store)
        mail(store, worker, "go")

        assert worker.run(max_polls=1) == 1
        log = log_of(worker)
        assert "this client has never sent that id" in log
        assert "4242" in log

    def test_a_late_response_to_an_id_we_did_send_says_so_instead(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """An id inside the range we have issued means the server answered a
        call we had already given up on — a timeout that is too short, not id
        confusion. The two read differently and must print differently."""
        _stub, worker = make_worker(self._inject(b'{"id": 1, "result": {"late": true}}'))
        register_sender(store)
        mail(store, worker, "go")

        assert worker.run(max_polls=1) == 1
        assert "a LATE response to request id 1" in log_of(worker)

    @pytest.mark.parametrize("params", ['"active"', "[1, 2]", "7"])
    def test_a_notification_whose_params_are_not_an_object_does_not_kill_the_turn(
        self, make_worker: MakeWorker, store: StateStore, params: str
    ) -> None:
        """Before this, the worker called ``.get()`` on a string inside
        `thread/status/changed` and died with an AttributeError in the middle of
        a turn — the sender got nothing and the queue behind it went too."""
        frame = f'{{"method": "{THREAD_STATUS_CHANGED}", "params": {params}}}'.encode()
        _stub, worker = make_worker(self._inject(frame))
        register_sender(store)
        mail(store, worker, "go")

        assert worker.run(max_polls=1) == 1
        assert replies(store)[0].body == "the turn carried on"
        assert "with a `params` member that is a" in log_of(worker)
        assert "not a JSON object" in log_of(worker)

    def test_a_notification_with_no_params_at_all_is_legal_and_silent(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """Omitting `params` is allowed by JSON-RPC, so it must NOT be reported
        as a fault: a fault log that cries at legal traffic is one nobody
        reads."""
        _stub, worker = make_worker(
            self._inject(f'{{"method": "{THREAD_STATUS_CHANGED}"}}'.encode())
        )
        register_sender(store)
        mail(store, worker, "go")

        assert worker.run(max_polls=1) == 1
        log = log_of(worker)
        assert "PROTOCOL FAULT" not in log
        assert "unmapped" in log, "an empty status payload is unmapped, not a protocol fault"

    def test_an_approval_request_with_malformed_params_is_declined_not_approved(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """The dangerous direction. A request this policy cannot parse must not
        become an approval, and the wire answer must be a real enum member —
        `abort` for the v1 family, which has no `decline` in it at all."""

        def stream(stub: StubServer, msg: dict[str, Any]) -> None:
            stub.send({"id": "ap-9", "method": "execCommandApproval", "params": "rm -rf /"})
            stub.wait_for(lambda: bool(stub.client_replies))
            stub.notify("turn/completed", {})

        stub, worker = make_worker(app_server(stream))
        register_sender(store)
        mail(store, worker, "go")

        assert worker.run(max_polls=1) == 1
        assert stub.client_replies[0]["result"] == {"decision": "abort"}
        log = log_of(worker)
        assert "DECLINE execCommandApproval" in log
        assert "params carried no readable command" in log
        # The two lines belong together: one says the params were not an
        # object, the next says what the policy did about it.
        assert "with a `params` member that is a str" in log

    def test_an_accepted_turn_with_no_terminal_event_is_reported_as_unfinished(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """Accepted is not completed — the spike reported one as the other for a
        whole run. The log has to say the deadline expired, and must not carry a
        terminal event it never saw."""

        def silent(stub: StubServer, msg: dict[str, Any]) -> None:
            stub.notify("turn/started", {})

        _stub, worker = make_worker(app_server(silent))
        worker.client.turn_timeout = 0.4
        register_sender(store)
        mail(store, worker, "hang please")

        assert worker.run(max_polls=1) == 1
        (reply,) = replies(store)
        assert "the turn did not finish before the worker's deadline" in reply.body
        log = log_of(worker)
        assert "terminal=(deadline expired)" in log
        assert "turn/completed" not in log


# ------------------------------------------------------- bus and filesystem


class TestBusAndFilesystem:
    def test_an_enormous_body_is_forwarded_whole_and_not_dumped_into_the_log(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """A megabyte of mail must reach the model intact (the WebSocket
        8-byte length path) and must not be copied into a log that a human is
        expected to read to the end."""
        body = "x" * 1_000_000 + " END-OF-ENORMOUS-BODY"
        stub, worker = make_worker()
        register_sender(store)
        mail(store, worker, body)

        assert worker.run(max_polls=1) == 1
        assert body in turn_text(frames(stub, "turn/start")[0]), "the body must go in verbatim"
        assert replies(store)[0].body == "looks good to me"
        log = log_of(worker)
        assert body not in log
        assert len(log) < 5_000, f"the log grew to {len(log)} chars from one message"

    def test_an_empty_body_still_runs_a_turn_and_still_replies(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        stub, worker = make_worker()
        register_sender(store)
        original = mail(store, worker, "")

        assert worker.run(max_polls=1) == 1
        assert original.msg_id in turn_text(frames(stub, "turn/start")[0])
        assert replies(store)[0].body == "looks good to me"
        assert "(empty)" in log_of(worker)

    @pytest.mark.parametrize(
        ("name", "corrupt"),
        [
            ("invalid utf-8", b'{"msg_id": "msg-1", "body": "\xff\xfe\xfd"}\n'),
            ("a line truncated by a crashed writer", b'{"msg_id": "msg-1", "from_agent": "age'),
        ],
    )
    def test_an_unreadable_inbox_stops_the_worker_and_names_the_file(
        self,
        make_spawned_worker: SpawnWorker,
        store: StateStore,
        name: str,
        corrupt: bytes,
    ) -> None:
        """Before this the pydantic ValidationError (or UnicodeDecodeError) came
        out of `pending()` as a bare traceback with nothing in the log. Worse
        would have been catching it and carrying on: the worker would poll an
        inbox that answers with an exception forever — alive, ingesting nothing,
        which is issue #55 exactly."""
        worker = make_spawned_worker()
        inbox = inbox_of(store, worker)
        inbox.write_bytes(corrupt)

        assert worker.serve() == 1, f"an inbox with {name} must not be survivable in silence"

        log = log_of(worker)
        assert "THE INBOX COULD NOT BE READ and the worker is stopping" in log
        assert str(inbox) in log
        assert "check the last line for a truncated write" in log
        assert not registered(store, worker)

    def test_a_deleted_inbox_is_not_fatal_and_later_mail_still_runs(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """Deleting the file is not corruption: an absent inbox is an empty one,
        and the worker has to stay up for the mail that comes next."""
        stub, worker = make_worker()
        register_sender(store)
        mail(store, worker, "first")
        assert worker.run(max_polls=1) == 1

        inbox_of(store, worker).unlink()
        assert worker.poll_once() == 0

        mail(store, worker, "second")
        assert worker.run(max_polls=1) == 1
        assert len(frames(stub, "turn/start")) == 2

    def test_mail_arriving_during_a_turn_runs_as_the_next_turn_in_order(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """Two senders, one turn in flight. The second message must run as its
        own turn, after the first, and neither may be lost."""
        holder: list[CodexWorker] = []

        def echo_and_post(stub: StubServer, msg: dict[str, Any]) -> None:
            body = turn_text(msg).splitlines()[-1]
            if body == "first":
                mail(store, holder[0], "second")
            stub.notify(AGENT_MESSAGE_DELTA, {"delta": f"echo:{body}"})
            stub.notify("turn/completed", {})

        stub, worker = make_worker(app_server(echo_and_post))
        holder.append(worker)
        register_sender(store)
        mail(store, worker, "first")

        assert worker.run(max_polls=2) == 2
        turns = frames(stub, "turn/start")
        assert [turn_text(t).splitlines()[-1] for t in turns] == ["first", "second"]
        assert [r.body for r in replies(store)] == ["echo:first", "echo:second"]

    @not_root
    def test_a_log_that_cannot_be_written_refuses_to_start(
        self, store: StateStore, worker_cwd: Path, state_root: Path
    ) -> None:
        """The log is not optional: every approval this worker grants on behalf
        of an unauthenticated sender is recorded there. It is also where a failed
        start explains itself, so a log that fails on first write turns a
        diagnosable error into a traceback with nothing written down."""
        state_root.chmod(0o500)
        try:
            with pytest.raises(WorkerLogUnwritable) as excinfo:
                CodexWorker(WorkerConfig(name="reviewer", cwd=worker_cwd), store=store)
        finally:
            state_root.chmod(0o700)
        message = str(excinfo.value)
        assert "cannot be written" in message
        assert "The worker refuses to start without it" in message
        assert "$SPANREED_STATE_ROOT" in message

    @not_root
    def test_a_read_only_state_root_is_a_sentence_on_stderr_not_a_traceback(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        worker_cwd: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The whole bus lives under the state root. If it cannot be written the
        worker has no registry row, no inbox and no log — and the only channel
        back from the machine this ships to is a pasted screen."""
        root = tmp_path / "read-only-state"
        root.mkdir()
        root.chmod(0o500)
        monkeypatch.setenv("SPANREED_STATE_ROOT", str(root))
        try:
            code = cli.main(["codex", "--name", "reviewer", "--cwd", str(worker_cwd)])
        finally:
            root.chmod(0o700)
        assert code == 1
        err = capsys.readouterr().err
        assert err.startswith("spanreed codex: ")
        assert "Traceback" not in err
        assert str(root) in err


# ----------------------------------------------------------------- the cwd


class TestCwdBoundary:
    """`--cwd` is the worker's entire security boundary. None of these weaken
    it; they ask what happens when the directory itself misbehaves."""

    def test_a_symlinked_cwd_is_resolved_once_and_bounds_both_ways(
        self, make_worker: MakeWorker, store: StateStore, tmp_path: Path
    ) -> None:
        """The link is resolved at construction, so the sandbox is scoped to the
        real directory and an approval naming either spelling gets the same
        answer. Resolving late — or not at all — would make the boundary depend
        on which name the request happened to use."""
        real = tmp_path / "real-repo"
        real.mkdir()
        link = tmp_path / "link-to-repo"
        link.symlink_to(real)
        outside = tmp_path / "elsewhere"
        outside.mkdir()

        def approvals(stub: StubServer, msg: dict[str, Any]) -> None:
            stub.ask("ap-1", "execCommandApproval", {"command": ["ls"], "cwd": str(link)})
            stub.wait_for(lambda: len(stub.client_replies) >= 1)
            stub.ask("ap-2", "execCommandApproval", {"command": ["ls"], "cwd": str(outside)})
            stub.wait_for(lambda: len(stub.client_replies) >= 2)
            stub.notify("turn/completed", {})

        stub, worker = make_worker(app_server(approvals), cwd=link)
        assert worker.config.cwd == real.resolve()
        register_sender(store)
        mail(store, worker, "go")
        worker.run(max_polls=1)

        assert params_of(stub, "turn/start")["sandboxPolicy"]["writableRoots"] == [str(real)]
        assert [r["result"]["decision"] for r in stub.client_replies] == ["approved", "abort"]

    def test_a_cwd_deleted_after_start_refuses_turns_and_says_why(
        self, make_worker: MakeWorker, store: StateStore, worker_cwd: Path
    ) -> None:
        """Before this the worker ran the turn anyway: it approved paths against
        a tree that no longer existed and handed Codex a cwd it could not enter,
        so the only symptom was a model apologising. The boundary being gone is
        the worker's business, not the model's."""
        stub, worker = make_worker()
        worker_cwd.rmdir()
        register_sender(store)
        original = mail(store, worker, "do the thing")

        assert worker.run(max_polls=1) == 1
        assert frames(stub, "turn/start") == [], "no turn may run without the boundary"
        (reply,) = replies(store)
        assert "is no longer a directory" in reply.body
        assert reply.in_reply_to == original.msg_id
        log = log_of(worker)
        assert "IS NO LONGER A DIRECTORY" in log
        assert "No turn was started and nothing was retried" in log
        assert registered(store, worker), "the worker stays on the bus; the directory may return"

    @not_root
    def test_a_cwd_that_cannot_be_written_warns_at_startup_in_write_modes(
        self, make_worker: MakeWorker, worker_cwd: Path
    ) -> None:
        """Workspace mode tells Codex it may write there. If it cannot, every
        edit fails inside the sandbox where the only visible symptom is the
        model reporting its own failure — so the reason is named once, up top."""
        worker_cwd.chmod(0o500)
        try:
            # Different names, so the two workers do not share one log file.
            _stub, worker = make_worker(mode="workspace", name="writer")
            _stub2, readonly = make_worker(mode="read-only", name="reader")
        finally:
            worker_cwd.chmod(0o700)
        log = log_of(worker)
        assert "is NOT WRITABLE by this process" in log
        assert "--mode read-only" in log
        assert "is NOT WRITABLE by this process" not in log_of(readonly), (
            "read-only work in a directory this user cannot write is legitimate"
        )


# ------------------------------------------------------------------- auth


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestAuthFaults:
    """`account/chatgptAuthTokens/refresh` arrives after a 401 and app-server
    blocks on the answer with no timeout. Every broken `auth.json` must produce
    a decline (never an invented token), a log line saying so, and no token
    value anywhere in that log."""

    @staticmethod
    def _break_missing(path: Path) -> None:
        pass

    @staticmethod
    def _break_truncated_json(path: Path) -> None:
        # Truncated mid-token: the file the CLI was writing when it was killed.
        # It contains a live-looking token, which is the point — the decline
        # must not echo the file back.
        _write_text(path, '{"tokens": {"access_token": "' + SECRET_TOKEN + '"')

    @staticmethod
    def _break_not_an_object(path: Path) -> None:
        _write_text(path, f'"{SECRET_TOKEN}"')

    @staticmethod
    def _break_unreadable(path: Path) -> None:
        write_auth(path)
        path.chmod(0o000)

    @staticmethod
    def _break_is_a_directory(path: Path) -> None:
        path.mkdir(parents=True)

    @staticmethod
    def _break_token_is_not_a_string(path: Path) -> None:
        write_auth(path, tokens={"access_token": 12345, "account_id": "acct-9f3"})

    @staticmethod
    def _break_token_is_blank(path: Path) -> None:
        write_auth(path, tokens={"access_token": "   ", "account_id": "acct-9f3"})

    @staticmethod
    def _break_no_account_id(path: Path) -> None:
        write_auth(path, tokens={"access_token": SECRET_TOKEN})

    @pytest.mark.parametrize(
        "how",
        [
            "_break_missing",
            "_break_truncated_json",
            "_break_not_an_object",
            pytest.param("_break_unreadable", marks=not_root),
            "_break_is_a_directory",
            "_break_token_is_not_a_string",
            "_break_token_is_blank",
            "_break_no_account_id",
        ],
    )
    def test_a_broken_auth_file_declines_loudly_and_leaks_nothing(
        self, make_worker: MakeWorker, tmp_path: Path, how: str
    ) -> None:
        path = tmp_path / "codex-home" / "auth.json"
        breaker: Callable[[Path], None] = getattr(self, how)
        breaker(path)
        _stub, worker = make_worker()
        try:
            answer = worker.handle_server_request(AUTH_TOKENS_REFRESH, {"reason": "unauthorized"})
        finally:
            if path.is_file():
                path.chmod(0o600)

        assert answer is None, "declining is -32601; inventing a token fails later and further away"
        log = log_of(worker)
        assert "AUTH REFRESH" in log and "DECLINED" in log
        assert str(path) in log, "the decline must name the file a reader has to fix"
        assert SECRET_TOKEN not in log
        for length in (8, 12, 16):
            assert SECRET_TOKEN[:length] not in log

    def test_the_missing_file_decline_says_what_to_run(
        self, make_worker: MakeWorker, tmp_path: Path
    ) -> None:
        """A decline that names the fix is worth more than one that names the
        error, on a machine where the reader is not the author."""
        _stub, worker = make_worker()
        assert worker.handle_server_request(AUTH_TOKENS_REFRESH, {"reason": "unauthorized"}) is None
        assert "run `codex login` for the account this worker should use" in log_of(worker)

    def test_a_partial_token_pair_names_the_missing_key(
        self, make_worker: MakeWorker, tmp_path: Path
    ) -> None:
        write_auth(tmp_path / "codex-home" / "auth.json", tokens={"account_id": "acct-9f3"})
        _stub, worker = make_worker()
        assert worker.handle_server_request(AUTH_TOKENS_REFRESH, {"reason": "unauthorized"}) is None
        log = log_of(worker)
        assert "tokens.access_token" in log
        assert "Declining rather than inventing a token" in log


class TestThreadStartShapes:
    """`thread/start` has answered with the id in two different shapes across
    versions. The worker used to read only the flat one while the doctor read
    both — so on a server answering the other shape, `--doctor` would have
    reported the machine healthy and the worker would have died at startup on
    the same call. The two must agree."""

    @pytest.mark.parametrize(
        "result",
        [
            {"threadId": "t-flat"},
            {"thread": {"id": "t-nested"}},
            {"thread": {"threadId": "t-nested-2"}},
        ],
    )
    def test_both_documented_thread_id_shapes_start_a_worker(
        self, make_worker: MakeWorker, store: StateStore, result: dict[str, Any]
    ) -> None:
        def shaped(stub: StubServer, msg: dict[str, Any]) -> None:
            method = msg.get("method")
            if method == "initialize":
                stub.reply(msg["id"], {"serverInfo": {"name": "stub"}})
            elif method == "thread/start":
                stub.reply(msg["id"], result)
            elif method == "turn/start":
                stub.reply(msg["id"], {"status": "inProgress"})
                stub.notify(AGENT_MESSAGE_DELTA, {"delta": "ok"})
                stub.notify("turn/completed", {})
            else:
                stub.reply(msg["id"], {"ok": method})

        stub, worker = make_worker(shaped)
        register_sender(store)
        mail(store, worker, "go")
        assert worker.run(max_polls=1) == 1
        expected = result.get("threadId") or next(iter(result["thread"].values()))
        assert params_of(stub, "turn/start")["threadId"] == expected

    def test_a_thread_start_result_with_no_id_anywhere_fails_to_start(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """And when neither shape is there, the failure names every key it
        looked for and prints what it actually got — a worker with no thread id
        cannot run one message, so this belongs at startup, not at the first
        piece of mail."""

        def idless(stub: StubServer, msg: dict[str, Any]) -> None:
            if msg.get("method") == "initialize":
                stub.reply(msg["id"], {"serverInfo": {"name": "stub"}})
            else:
                stub.reply(msg["id"], {"status": "created"})

        _stub, worker = make_worker(idless, start=False)
        assert worker.serve() == 1
        log = log_of(worker)
        assert "FAILED TO START" in log
        assert "no thread id in any shape this worker knows" in log
        assert "'status': 'created'" in log, "the log must show what the server actually sent"
        assert not registered(store, worker)


@not_root
def test_a_log_that_becomes_unwritable_mid_run_keeps_the_line(tmp_path: Path) -> None:
    """The reporting path must survive its own subject.

    Every failure in this module reports itself by calling ``WorkerLog.write``.
    If that raised — a full disk, a permission change, an unmounted state root
    — the report would die on the way out and take its own cause with it. The
    line goes to the stream instead, and says the file no longer has it.
    """
    directory = tmp_path / "codex"
    stream = io.StringIO()
    path = directory / "reviewer.log"
    log = WorkerLog(path, stream)
    log.write("[codex-approval] APPROVE execCommandApproval subject=ls")
    # The *file*, not the directory: directory bits govern creating entries, so
    # a read-only directory leaves an already-open-able file perfectly writable
    # — which is how the first version of this test passed against a log that
    # was never actually broken.
    path.chmod(0o400)
    try:
        log.write("[codex-approval] DECLINE execCommandApproval subject=rm -rf junk")
    finally:
        path.chmod(0o600)

    text = stream.getvalue()
    assert "DECLINE execCommandApproval subject=rm -rf junk" in text, "the line must not be lost"
    assert "COULD NOT BE WRITTEN" in text
    assert "approvals are no longer being recorded anywhere durable" in text
    assert log.failures == 1
    # And the file still holds what it took before the fault, unharmed.
    assert "APPROVE execCommandApproval subject=ls" in path.read_text()
