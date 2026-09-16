"""Tests for the Codex worker.

``codex`` is not installed on this machine or on CI, so everything here runs
against the stub app-server in ``test_codex_client.py`` — the stdlib RFC 6455
server on a unix socket. It is imported rather than re-implemented: a second
stub would be a second thing to keep honest, and this one already encodes the
protocol's sharp edges (server→client requests answered on their own thread,
notifications interleaved with responses).

Most of these exist because of a specific way this worker could be silently
wrong:

- ``--cwd`` missing → refuse. It is the only bound on an unauthenticated
  sender's ability to run commands.
- ``--mode danger`` → warn at startup **and on every turn**. The owner allowed
  the mode on condition the warning is impossible to miss.
- approvals → both outcomes in the log file. An auto-approved command that
  appears nowhere is the one that cannot be reviewed.
- ``effort`` → on ``turn/start``, never on ``thread/start``, which accepts it
  and ignores it.
- a turn that fails → the sender gets the error. Silence leaves a peer blocked
  on a reply that is never coming.
- auth refresh → answered from ``auth.json``, and the token value never reaches
  the log.
"""

from __future__ import annotations

import ast
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from spanreed import cli
from spanreed.codex_approvals import CONFINED_MODES, MODES
from spanreed.codex_worker import (
    AGENT_MESSAGE_DELTA,
    AUTH_TOKENS_REFRESH,
    BOUNDARY_BY_MODE,
    DANGER_BANNER,
    RATE_LIMITS_UPDATED,
    SANDBOX_MODES,
    THREAD_STATUS_CHANGED,
    CodexWorker,
    WorkerConfig,
)
from spanreed.protocol import Message
from spanreed.store import StateStore
from tests.unit.test_codex_client import Handler, StubServer

SENDER = "agent-sender"
THREAD_ID = "t-worker-1"
SECRET_TOKEN = "sk-not-a-real-token-but-treat-it-like-one"

TurnStream = Callable[[StubServer, dict[str, Any]], None]
MakeWorker = Callable[..., tuple[StubServer, CodexWorker]]


# ------------------------------------------------------------- stub wiring


def app_server(turn: TurnStream | None = None) -> Handler:
    """A stub that answers the calls a worker makes.

    ``turn`` runs once ``turn/start`` has been accepted and owns everything the
    turn streams; the default streams one agent message and completes.
    """

    def stream_hello(stub: StubServer, msg: dict[str, Any]) -> None:
        stub.notify(AGENT_MESSAGE_DELTA, {"delta": "looks "})
        stub.notify(AGENT_MESSAGE_DELTA, {"delta": "good to me"})
        stub.notify("turn/completed", {"usage": {"input": 1}})

    streaming = turn if turn is not None else stream_hello

    def handler(stub: StubServer, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method == "initialize":
            stub.reply(msg["id"], {"serverInfo": {"name": "stub-app-server", "version": "0"}})
        elif method == "thread/start":
            stub.reply(msg["id"], {"threadId": THREAD_ID})
        elif method == "turn/start":
            # turn/start returns on *acceptance*; the output arrives afterwards
            # as notifications.
            stub.reply(msg["id"], {"status": "inProgress"})
            streaming(stub, msg)
        else:
            stub.reply(msg["id"], {"ok": method})

    return handler


def frames(stub: StubServer, method: str) -> list[dict[str, Any]]:
    """Every frame of one method the worker sent, in order."""
    return [m for m in stub.received if m.get("method") == method]


def params_of(stub: StubServer, method: str) -> dict[str, Any]:
    """Params of the first frame of ``method``."""
    return frames(stub, method)[0]["params"]


def turn_text(frame: dict[str, Any]) -> str:
    """The text a ``turn/start`` frame carried."""
    return str(frame["params"]["input"][0]["text"])


def register_sender(store: StateStore) -> None:
    """Put the sender on the registry — a reply to an unknown id is refused."""
    store.register_agent(name="sender", working_dir="/tmp", pid=os.getpid(), agent_id=SENDER)


def mail(store: StateStore, worker: CodexWorker, body: str) -> Message:
    return store.send_message(from_agent=SENDER, to_agent=worker.config.agent_id, body=body)


def replies(store: StateStore) -> list[Message]:
    return store.recv_messages(SENDER)


def log_of(worker: CodexWorker) -> str:
    return worker.log.path.read_text()


# ------------------------------------------------------------------- --cwd


class TestCwdIsRequired:
    """``--cwd`` has no default — not $HOME, not the process cwd, not whatever
    config.toml marks trusted. The machine this design was validated on marks
    all of $HOME trusted, so an inherited default hands over the home dir."""

    def test_cli_refuses_without_cwd(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert cli.main(["codex", "--name", "reviewer"]) == 2
        err = capsys.readouterr().err
        assert "--cwd is required and has no default" in err
        # The refusal has to carry the reason, not just the rule.
        assert "authenticate" in err

    def test_cli_refuses_a_cwd_that_is_not_a_directory(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        missing = tmp_path / "nope"
        assert cli.main(["codex", "--name", "r", "--cwd", str(missing)]) == 2
        assert "existing directory" in capsys.readouterr().err

    def test_config_refuses_a_missing_directory(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="existing directory"):
            WorkerConfig(name="reviewer", cwd=tmp_path / "gone")

    def test_config_resolves_cwd_to_an_absolute_path(
        self, monkeypatch: pytest.MonkeyPatch, worker_cwd: Path
    ) -> None:
        """A relative --cwd resolved later would be resolved against the wrong
        process's directory; codex_approvals.contains() refuses one outright."""
        monkeypatch.chdir(worker_cwd.parent)
        config = WorkerConfig(name="reviewer", cwd=Path(worker_cwd.name))
        assert config.cwd == worker_cwd.resolve()

    def test_bad_mode_is_refused(self, worker_cwd: Path) -> None:
        with pytest.raises(ValueError, match="--mode"):
            WorkerConfig(name="reviewer", cwd=worker_cwd, mode="yolo")


# ------------------------------------------------------------------ startup


class TestStartup:
    def test_thread_start_sends_sandbox_mode_not_sandbox_policy(
        self, make_worker: MakeWorker
    ) -> None:
        """ClientRequest.json: thread/start takes `sandbox` (a SandboxMode
        *enum*); the SandboxPolicy *object* belongs on turn/start under
        `sandboxPolicy`. Sending the object under the enum's name is the kind of
        mistake app-server accepts and ignores."""
        stub, _worker = make_worker()
        params = params_of(stub, "thread/start")
        assert params["sandbox"] == "workspace-write"
        assert "sandboxPolicy" not in params
        assert params["approvalPolicy"] == "on-request"
        assert params["cwd"] == str(_worker.config.cwd)

    @pytest.mark.parametrize(
        ("mode", "sandbox", "policy"),
        [
            ("workspace", "workspace-write", "on-request"),
            ("danger", "danger-full-access", "never"),
        ],
    )
    def test_mode_maps_to_the_schema_values(
        self, make_worker: MakeWorker, mode: str, sandbox: str, policy: str
    ) -> None:
        stub, _worker = make_worker(mode=mode)
        params = params_of(stub, "thread/start")
        assert (params["sandbox"], params["approvalPolicy"]) == (sandbox, policy)

    def test_instructions_are_additive_developer_instructions(
        self, make_worker: MakeWorker
    ) -> None:
        """`developerInstructions`, not `baseInstructions`: the latter replaces
        Codex's built-in agent prompt, so a one-line persona would strip the
        model's tool guidance."""
        stub, _worker = make_worker(instructions="Be terse. Review Kotlin only.")
        params = params_of(stub, "thread/start")
        assert "baseInstructions" not in params
        assert "Be terse. Review Kotlin only." in params["developerInstructions"]
        assert "Spanreed bus worker" in params["developerInstructions"]

    def test_worker_registers_on_the_bus(self, make_worker: MakeWorker, store: StateStore) -> None:
        _stub, worker = make_worker()
        agents = {a.agent_id: a for a in store.list_agents()}
        entry = agents[worker.config.agent_id]
        assert entry.name == "reviewer"
        assert entry.working_dir == str(worker.config.cwd)
        assert entry.pid == os.getpid()
        assert entry.status == "idle"

    def test_close_leaves_the_bus(self, make_worker: MakeWorker, store: StateStore) -> None:
        _stub, worker = make_worker()
        worker.close()
        assert all(a.agent_id != worker.config.agent_id for a in store.list_agents())

    def test_restart_starts_a_fresh_thread(self, make_worker: MakeWorker) -> None:
        """A restarted worker does not resume the old thread (owner decision)."""
        stub, _worker = make_worker()
        assert "thread/resume" not in stub.methods
        assert len(frames(stub, "thread/start")) == 1


class TestDangerMode:
    """`--mode danger` is allowed with any sender, but must warn loudly — at
    startup *and* on every turn. That was the owner's explicit choice."""

    def test_warns_at_startup(self, make_worker: MakeWorker) -> None:
        _stub, worker = make_worker(mode="danger")
        assert DANGER_BANNER in log_of(worker)
        assert "DANGER MODE" in log_of(worker)

    def test_warns_again_on_every_turn(self, make_worker: MakeWorker, store: StateStore) -> None:
        _stub, worker = make_worker(mode="danger")
        register_sender(store)
        mail(store, worker, "first")
        mail(store, worker, "second")
        worker.run(max_polls=1)
        # Once at startup, once per turn: a log the owner scrolls through must
        # say what mode the command they are reading ran under.
        assert log_of(worker).count(DANGER_BANNER) == 3

    def test_confined_modes_do_not_warn(self, make_worker: MakeWorker) -> None:
        _stub, worker = make_worker(mode="workspace")
        assert "DANGER MODE" not in log_of(worker)


# ---------------------------------------------------------------- approvals


def _approval_turn(inside: str, outside: str) -> TurnStream:
    """A turn that asks for one command inside --cwd and one outside it."""

    def stream(stub: StubServer, msg: dict[str, Any]) -> None:
        stub.ask("ap-1", "execCommandApproval", {"command": ["ls", "-la"], "cwd": inside})
        stub.wait_for(lambda: len(stub.client_replies) >= 1)
        stub.ask("ap-2", "execCommandApproval", {"command": ["rm", "-rf", "junk"], "cwd": outside})
        stub.wait_for(lambda: len(stub.client_replies) >= 2)
        stub.notify("turn/completed", {})

    return stream


class TestApprovals:
    def test_inside_cwd_approved_outside_declined_and_both_logged(
        self, make_worker: MakeWorker, store: StateStore, worker_cwd: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        stub, worker = make_worker(app_server(_approval_turn(str(worker_cwd), str(outside))))
        register_sender(store)
        mail(store, worker, "do the thing")
        worker.run(max_polls=1)

        decisions = [r["result"]["decision"] for r in stub.client_replies]
        # execCommandApproval is v1, whose response type is ReviewDecision:
        # approved | approved_for_session | approved_mcp_policy_amendment |
        # timed_out | abort. There is no "decline" in it at all -- the negative
        # is `abort`. This test previously asserted ["approve", "decline"],
        # neither of which is a member of that enum, so it was pinning the bug
        # in place: a server given an invalid value errors or ignores it, and
        # the worker logs an approval that never took effect.
        assert decisions == ["approved", "abort"]

        log = log_of(worker)
        assert "APPROVE execCommandApproval" in log
        assert "DECLINE execCommandApproval" in log
        # The log has to name *what* ran, not just that something did.
        assert "ls -la" in log
        assert "rm -rf junk" in log
        assert str(outside) in log

    def test_elicitation_is_declined_with_an_action(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """A worker has no human, so an elicitation cannot be answered. Silence
        would hang the turn forever — there is no server-side timeout."""

        def stream(stub: StubServer, msg: dict[str, Any]) -> None:
            stub.ask(
                "el-1",
                "mcpServer/elicitation/request",
                {"mode": "form", "message": "Which branch?", "requestedSchema": {}},
            )
            stub.wait_for(lambda: bool(stub.client_replies))
            stub.notify("turn/completed", {})

        stub, worker = make_worker(app_server(stream))
        register_sender(store)
        mail(store, worker, "ship it")
        worker.run(max_polls=1)
        assert stub.client_replies[0]["result"] == {"action": "decline"}
        assert "mcpServer/elicitation/request" in log_of(worker)

    def test_unknown_server_request_is_refused_not_dropped(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        def stream(stub: StubServer, msg: dict[str, Any]) -> None:
            stub.ask("x-1", "some/methodNobodyImplements", {})
            stub.wait_for(lambda: bool(stub.client_replies))
            stub.notify("turn/completed", {})

        stub, worker = make_worker(app_server(stream))
        register_sender(store)
        mail(store, worker, "hello")
        worker.run(max_polls=1)
        assert stub.client_replies[0]["error"]["code"] == -32601
        assert "unhandled server request" in log_of(worker)


# -------------------------------------------------------------------- turns


class TestTurns:
    def test_effort_rides_on_turn_start_and_never_on_thread_start(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """thread/start accepts `effort` and ignores it, so a worker-level
        effort has to be re-applied on every turn or it is silently absent."""
        stub, worker = make_worker(effort="medium", model="gpt-5.6-sol")
        register_sender(store)
        mail(store, worker, "one")
        mail(store, worker, "two")
        worker.run(max_polls=1)

        assert "effort" not in params_of(stub, "thread/start")
        assert params_of(stub, "thread/start")["model"] == "gpt-5.6-sol"
        turns = frames(stub, "turn/start")
        assert len(turns) == 2
        for turn in turns:
            assert turn["params"]["effort"] == "medium"
            assert turn["params"]["model"] == "gpt-5.6-sol"

    def test_turn_carries_the_scoped_sandbox_policy(
        self, make_worker: MakeWorker, store: StateStore, worker_cwd: Path
    ) -> None:
        """Only the turn-level policy object carries writableRoots, which is
        what scopes writes to --cwd."""
        stub, worker = make_worker()
        register_sender(store)
        mail(store, worker, "one")
        worker.run(max_polls=1)
        policy = params_of(stub, "turn/start")["sandboxPolicy"]
        assert policy["type"] == "workspaceWrite"
        assert policy["writableRoots"] == [str(worker_cwd)]
        assert policy["networkAccess"] is False

    def test_reply_goes_back_to_the_sender(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        _stub, worker = make_worker()
        register_sender(store)
        original = mail(store, worker, "review the diff")
        assert worker.run(max_polls=1) == 1

        (reply,) = replies(store)
        assert reply.body == "looks good to me"
        assert reply.from_agent == worker.config.agent_id
        assert reply.to_agent == SENDER
        assert reply.in_reply_to == original.msg_id

    def test_the_turn_carries_the_body_and_says_it_is_mail(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        stub, worker = make_worker()
        register_sender(store)
        original = mail(store, worker, "please review PR 42")
        worker.run(max_polls=1)
        text = turn_text(frames(stub, "turn/start")[0])
        assert "please review PR 42" in text
        assert SENDER in text
        assert original.msg_id in text

    def test_two_messages_run_as_two_turns_in_order(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """FIFO, one turn per message, each with its own reply. Codex's native
        `steer` is deliberately not used: a steered turn produces one reply for
        two senders' messages, which the bus cannot express."""

        def echo(stub: StubServer, msg: dict[str, Any]) -> None:
            body = turn_text(msg).splitlines()[-1]
            stub.notify(AGENT_MESSAGE_DELTA, {"delta": f"echo:{body}"})
            stub.notify("turn/completed", {})

        stub, worker = make_worker(app_server(echo))
        register_sender(store)
        first = mail(store, worker, "first")
        second = mail(store, worker, "second")
        assert worker.run(max_polls=1) == 2

        turns = frames(stub, "turn/start")
        assert [turn_text(t).splitlines()[-1] for t in turns] == ["first", "second"]
        assert [(r.body, r.in_reply_to) for r in replies(store)] == [
            ("echo:first", first.msg_id),
            ("echo:second", second.msg_id),
        ]

    def test_a_message_is_not_run_twice(self, make_worker: MakeWorker, store: StateStore) -> None:
        """The cursor advances per message, so the next poll sees nothing."""
        stub, worker = make_worker()
        register_sender(store)
        mail(store, worker, "once")
        assert worker.run(max_polls=1) == 1
        assert worker.run(max_polls=1) == 0
        assert len(frames(stub, "turn/start")) == 1

    def test_status_is_idle_again_after_a_turn(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        _stub, worker = make_worker()
        register_sender(store)
        mail(store, worker, "work")
        worker.run(max_polls=1)
        entry = next(a for a in store.list_agents() if a.agent_id == worker.config.agent_id)
        assert entry.status == "idle"


class TestTurnFailure:
    """`turn/failed` / `turn/aborted`: the sender gets the error. No retry —
    a retry would re-run a turn whose side effects already happened, and
    silence would leave the sender blocked on a reply that never comes."""

    @pytest.mark.parametrize("terminal", ["turn/failed", "turn/aborted"])
    def test_failure_produces_an_error_reply(
        self, make_worker: MakeWorker, store: StateStore, terminal: str
    ) -> None:
        def fail(stub: StubServer, msg: dict[str, Any]) -> None:
            stub.notify(AGENT_MESSAGE_DELTA, {"delta": "I started, then "})
            stub.notify(terminal, {"error": {"message": "model exploded"}})

        _stub, worker = make_worker(app_server(fail))
        register_sender(store)
        original = mail(store, worker, "do it")
        worker.run(max_polls=1)

        (reply,) = replies(store)
        assert terminal in reply.body
        assert "model exploded" in reply.body
        # The partial output is not thrown away: it is often the diagnosis.
        assert "I started, then" in reply.body
        assert reply.in_reply_to == original.msg_id

    def test_a_turn_that_never_ends_still_replies(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """An accepted turn with no terminal event is a partial result, not a
        completed one (spike README, run 29 called it completed)."""

        def silent(stub: StubServer, msg: dict[str, Any]) -> None:
            stub.notify("turn/started", {})

        _stub, worker = make_worker(app_server(silent))
        worker.client.turn_timeout = 0.4
        register_sender(store)
        mail(store, worker, "hang please")
        worker.run(max_polls=1)

        (reply,) = replies(store)
        assert "did not finish" in reply.body

    def test_an_empty_turn_says_so_rather_than_replying_with_nothing(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        def quiet(stub: StubServer, msg: dict[str, Any]) -> None:
            stub.notify("turn/completed", {})

        _stub, worker = make_worker(app_server(quiet))
        register_sender(store)
        mail(store, worker, "say nothing")
        worker.run(max_polls=1)
        (reply,) = replies(store)
        assert "no agent message" in reply.body

    def test_a_refused_turn_replies_with_the_error(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """app-server refusing `turn/start` must not take the worker down: the
        queue behind this message would go with it."""

        def refuse(stub: StubServer, msg: dict[str, Any]) -> None:
            method = msg.get("method")
            if method == "initialize":
                stub.reply(msg["id"], {"serverInfo": {"name": "stub"}})
            elif method == "thread/start":
                stub.reply(msg["id"], {"threadId": THREAD_ID})
            elif method == "turn/start":
                stub.reply(msg["id"], error={"code": -32600, "message": "thread is busy"})
            else:
                stub.reply(msg["id"], {"ok": method})

        _stub, worker = make_worker(refuse)
        register_sender(store)
        mail(store, worker, "go")
        worker.run(max_polls=1)
        (reply,) = replies(store)
        assert "could not start" in reply.body
        assert "thread is busy" in reply.body
        assert worker.run(max_polls=1) == 0, "the message must not be retried"

    def test_a_failed_start_returns_one_and_leaves_no_registry_row(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        def broken(stub: StubServer, msg: dict[str, Any]) -> None:
            if msg.get("method") == "initialize":
                stub.reply(msg["id"], {"serverInfo": {"name": "stub"}})
            else:
                stub.reply(msg["id"], error={"code": -32603, "message": "no model configured"})

        _stub, worker = make_worker(broken, start=False)
        assert worker.serve() == 1
        assert all(a.agent_id != worker.config.agent_id for a in store.list_agents())
        assert "FAILED TO START" in log_of(worker)

    def test_an_undeliverable_reply_is_logged_and_does_not_stop_the_worker(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        """The sender deregistered while its turn ran. Losing the reply is bad;
        taking the worker down and losing the queue behind it is worse."""
        _stub, worker = make_worker()
        register_sender(store)
        mail(store, worker, "answer me")
        store.deregister_agent(SENDER)
        assert worker.run(max_polls=1) == 1
        log = log_of(worker)
        assert "COULD NOT BE DELIVERED" in log
        assert "looks good to me" in log


# ----------------------------------------------------------- the idle read


class TestIdleRead:
    """The doc claimed observed status and quota "fall out" of the
    notifications. They do not: a synchronous client only reads frames inside a
    call, so between turns an idle worker reads nothing and those notifications
    sit unread. This is that gap closed."""

    def test_status_notification_between_turns_reaches_the_registry(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        stub, worker = make_worker()
        assert stub.wait_for(lambda: stub.conn is not None)
        stub.notify(THREAD_STATUS_CHANGED, {"status": "active"})
        # No mail, so this poll is nothing but the idle read.
        assert worker.poll_once() == 0
        entry = next(a for a in store.list_agents() if a.agent_id == worker.config.agent_id)
        assert entry.status == "working"
        assert "observed thread status active" in log_of(worker)

    def test_rate_limits_between_turns_are_consumed_and_logged(
        self, make_worker: MakeWorker
    ) -> None:
        stub, worker = make_worker()
        assert stub.wait_for(lambda: stub.conn is not None)
        stub.notify(RATE_LIMITS_UPDATED, {"primary": {"usedPercent": 91}})
        worker.poll_once()
        assert worker.rate_limits == {"primary": {"usedPercent": 91}}
        assert "quota" in log_of(worker)
        assert "91" in log_of(worker)

    def test_an_unmapped_status_is_logged_not_guessed(
        self, make_worker: MakeWorker, store: StateStore
    ) -> None:
        stub, worker = make_worker()
        assert stub.wait_for(lambda: stub.conn is not None)
        stub.notify(THREAD_STATUS_CHANGED, {"status": "chartreuse"})
        worker.poll_once()
        entry = next(a for a in store.list_agents() if a.agent_id == worker.config.agent_id)
        assert entry.status == "idle", "an unknown value must not move the registry"
        assert "unmapped" in log_of(worker)


# ------------------------------------------------------------ auth refresh


def write_auth(path: Path, **overrides: Any) -> Path:
    """An ``auth.json`` shaped like the real one (spike README, run 25, which
    listed its keys: ``OPENAI_API_KEY, auth_mode, last_refresh, tokens``)."""
    payload: dict[str, Any] = {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "last_refresh": "2026-09-01T00:00:00Z",
        "tokens": {
            "id_token": "header.payload.sig",
            "access_token": SECRET_TOKEN,
            "refresh_token": "rt-also-secret",
            "account_id": "acct-9f3",
        },
    }
    payload.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))
    return path


def _auth_turn(stub: StubServer, msg: dict[str, Any]) -> None:
    """A turn that hits a 401 and asks the client for a token."""
    stub.ask("auth-1", AUTH_TOKENS_REFRESH, {"reason": "unauthorized", "previousAccountId": None})
    stub.wait_for(lambda: bool(stub.client_replies))
    stub.notify("turn/completed", {})


class TestAuthRefresh:
    def test_answers_from_auth_json(
        self, make_worker: MakeWorker, store: StateStore, tmp_path: Path
    ) -> None:
        write_auth(tmp_path / "codex-home" / "auth.json")
        stub, worker = make_worker(app_server(_auth_turn))
        register_sender(store)
        mail(store, worker, "go")
        worker.run(max_polls=1)
        assert stub.client_replies[0]["result"] == {
            "accessToken": SECRET_TOKEN,
            "chatgptAccountId": "acct-9f3",
        }

    def test_the_token_value_never_reaches_the_log(
        self, make_worker: MakeWorker, store: StateStore, tmp_path: Path
    ) -> None:
        """Not truncated, not hashed, not "for debugging"."""
        write_auth(tmp_path / "codex-home" / "auth.json")
        _stub, worker = make_worker(app_server(_auth_turn))
        register_sender(store)
        mail(store, worker, "go")
        worker.run(max_polls=1)
        log = log_of(worker)
        assert "AUTH REFRESH" in log, "the refresh itself must be visible"
        assert SECRET_TOKEN not in log
        assert "rt-also-secret" not in log
        for length in (8, 12, 16):
            assert SECRET_TOKEN[:length] not in log
        # The account id is an identifier, not a credential: the owner should be
        # able to see which account a worker charged.
        assert "acct-9f3" in log

    def test_missing_file_declines_loudly(self, make_worker: MakeWorker, store: StateStore) -> None:
        stub, worker = make_worker(app_server(_auth_turn))
        register_sender(store)
        mail(store, worker, "go")
        worker.run(max_polls=1)
        assert stub.client_replies[0]["error"]["code"] == -32601
        log = log_of(worker)
        assert "AUTH REFRESH" in log and "DECLINED" in log
        assert "codex login" in log

    def test_a_file_without_a_token_declines_and_names_what_is_missing(
        self, make_worker: MakeWorker, store: StateStore, tmp_path: Path
    ) -> None:
        write_auth(tmp_path / "codex-home" / "auth.json", tokens={"account_id": "acct-9f3"})
        stub, worker = make_worker(app_server(_auth_turn))
        register_sender(store)
        mail(store, worker, "go")
        worker.run(max_polls=1)
        assert stub.client_replies[0]["error"]["code"] == -32601
        assert "tokens.access_token" in log_of(worker)

    def test_answers_directly_without_a_turn(self, make_worker: MakeWorker, tmp_path: Path) -> None:
        """The handler is pure enough to call on its own — worth pinning, since
        a 401 can arrive during any call, not only during a turn."""
        write_auth(tmp_path / "codex-home" / "auth.json")
        _stub, worker = make_worker()
        assert worker.handle_server_request(AUTH_TOKENS_REFRESH, {"reason": "unauthorized"}) == {
            "accessToken": SECRET_TOKEN,
            "chatgptAccountId": "acct-9f3",
        }


class TestBoundaryInstructionMatchesTheMode:
    """What the model is told must match what the code enforces.

    The preamble used to claim unconditionally that writes outside --cwd are
    "declined by the worker before they reach you". That is false in EVERY mode,
    the default included: _decide_exec reads params["cwd"] and never what the
    command targets, so `rm -rf /elsewhere` launched from --cwd is approved. It
    is false a second way in danger, where approval_policy() is "never" and
    nothing is asked at all.

    An earlier version of this docstring said the claim was "true in workspace
    mode", which is the belief that produced rounds three and four of the review
    of #56 -- the defect kept reappearing in whichever branch nobody had
    executed. Nothing here is true in workspace mode.
    """

    def test_danger_does_not_promise_a_boundary_it_does_not_have(self) -> None:
        text = BOUNDARY_BY_MODE["danger"].format(cwd="/w")
        assert "NO SANDBOX" in text
        assert "declined by the worker" not in text
        # A phrase that does not span the wrap. "nothing constrains you" broke
        # across a line and the assertion failed on correct text -- the second
        # time that has happened in this repo, hence the note.
        assert "no mechanism will stop you" in text

    def test_workspace_describes_the_sandbox_that_actually_confines_it(self) -> None:
        text = BOUNDARY_BY_MODE["workspace"].format(cwd="/w")
        assert "workspaceWrite" in text
        assert "/w" in text

    # ---------------------------------------------------------------- claims
    # Three review rounds found three false sentences here, each in a branch the
    # previous round had not looked at, because nothing executed the prose. One
    # test per retracted claim: the code is mode-blind and the templates are
    # mode-specific, so the only thing keeping them honest is a list of things
    # they are not allowed to say.

    def test_no_branch_claims_the_worker_declines_by_path(self) -> None:
        # FALSE in every mode. _decide_exec reads params["cwd"] and never the
        # command's arguments, so `rm -rf /elsewhere` launched from --cwd is
        # approved. Said in the DEFAULT mode for two rounds.
        for mode, template in BOUNDARY_BY_MODE.items():
            text = template.format(cwd="/w")
            assert "declines approvals for paths outside" not in text, mode
            assert "declined by the worker" not in text, mode

    def test_no_branch_claims_the_worker_declines_everything(self) -> None:
        # decide() takes no mode and approves in-cwd requests in every one,
        # so no branch may claim the worker declines everything.
        for mode, template in BOUNDARY_BY_MODE.items():
            assert "declines every approval" not in template.format(cwd="/w"), mode

    def test_no_branch_asserts_what_an_approval_is_worth(self) -> None:
        # UNMEASURED. The vendored schema describes approvals as the channel for
        # "sandbox escapes" and carries applyNetworkPolicyAmendment, so an
        # approval plausibly CAN lift a sandbox restriction. Nothing here has
        # been run against a real app-server, so no branch may claim either way.
        for mode, template in BOUNDARY_BY_MODE.items():
            assert "buys you nothing" not in template.format(cwd="/w"), mode

    def test_every_branch_describes_what_is_SENT_not_what_is_enforced(self) -> None:
        # The durable form of all three: these templates may say what the worker
        # asks Codex for, because that is observable here. They may not assert
        # what Codex then does.
        # Derived, NOT a literal. The review added a fourth mode with a newly
        # worded false sentence and this class reported 9 passed, because the
        # loop iterated a hand-written list and never saw it.
        for mode in CONFINED_MODES:
            text = BOUNDARY_BY_MODE[mode].format(cwd="/w")
            # Presence: it must describe what is sent.
            assert "asks Codex for" in text, mode
            # Absence: and must not ALSO assert enforcement. The review injected
            # a template containing both and this test passed on the first half
            # alone -- the same defect it is named for, inside the guard suite.
            for claim in ("turned away by the worker", "before Codex ever sees", "sealed off"):
                assert claim not in text, f"{mode}: enforcement claim {claim!r}"

    def test_every_mode_has_a_boundary_sentence(self) -> None:
        # A mode added without one would fall back to a KeyError at start(),
        # which is loud -- but this makes the omission visible at test time.
        assert set(BOUNDARY_BY_MODE) == set(MODES)

    def test_every_mode_has_a_sandbox_mode(self) -> None:
        # SANDBOX_MODES is indexed with the same KeyError-at-start shape as
        # BOUNDARY_BY_MODE and had no test anywhere. Its sibling got one in the
        # commit that introduced it; leaving this half pinned is how the next
        # mode gets added with only one of the two tables updated.
        assert set(SANDBOX_MODES) == set(MODES)


class TestApprovalReachesTheLogFile:
    """decide() returning a Decision is not the same as the log receiving it.

    The ValueError gap in contains() was exactly this distinction: the request
    was still answered (-32603 from the client's handler wrapper), so nothing
    hung and nothing was unconfined — but log_line() never ran, and the approval
    existed nowhere. Pinning it at the decide() level would have missed that,
    which is why this drives a real worker and reads the file.
    """

    def test_an_embedded_nul_is_written_to_the_approval_log(
        self, make_worker: MakeWorker, worker_cwd: Path
    ) -> None:
        _stub, worker = make_worker(app_server())
        reply = worker.handle_server_request(
            "execCommandApproval", {"command": ["ls"], "cwd": "/etc\x00/passwd"}
        )
        # Answered on the wire with a real ReviewDecision member...
        assert reply == {"decision": "abort"}
        # ...and, the part that was missing, recorded.
        log = log_of(worker)
        assert "DECLINE execCommandApproval" in log
        assert worker.log.failures == 0


class TestNoBoundaryClaimInAnyEmittedString:
    """The guard's generator is "prose the worker emits", not one dict.

    Four review rounds, four instances, each somewhere the previous round's
    guard did not reach: the danger template, a since-removed read-only
    template, the workspace template, and then the --cwd-is-gone refusal 400
    lines away -- emitted both to the operator's log AND onto the bus, where a
    peer agent reads it exactly as the model reads the preamble.

    So this walks the AST and checks every string the module can emit, skipping
    docstrings: a docstring SAYING a phrase was retracted is this codebase
    documenting its own history, while the same phrase in an emitted string is
    the defect. A raw text scan cannot tell those apart and flagged the
    explanation as the crime.

    **THIS IS A REGRESSION RATCHET OVER PHRASINGS ALREADY SHIPPED, NOT A
    DECISION PROCEDURE FOR THE CLAIM.** The generator is total -- every emitted
    string in every listed module is walked -- and the *predicate* is a
    substring blacklist (see FORBIDDEN), so a newly-worded sentence making the same
    claim passes. The review of #56 demonstrated exactly that: a fresh sentence
    in the DEFAULT mode's preamble, green across the whole suite. It also
    demonstrated the one-word hole: "the only bound on what THE worker may
    touch" is blacklisted and "...what THIS worker may touch" was not, and
    shipped in cli.py.

    The docstring above argues completeness at length and all of it is about
    which *sites* are scanned. That is the half that was fixed. The predicate is
    the open half, and a structural close for it is recorded in
    docs/open-questions.md rather than half-built here.
    """

    FORBIDDEN = (
        "declined outside it",
        "granted inside it",
        # Both articles: "the" was blacklisted, "this" shipped in cli.py.
        "only bound on what the worker may touch",
        "only bound on what this worker may touch",
        "whole blast radius",
        "declines approvals for paths outside",
        "declines every approval",
        "buys you nothing",
    )

    @staticmethod
    def _emitted_strings(path: Path) -> list[str]:
        """Every string constant the module can emit, docstrings excluded."""
        tree = ast.parse(path.read_text())
        docstrings: set[int] = set()
        for node in ast.walk(tree):
            # Any string standing alone as a statement is documentation: a
            # module/class/function docstring, or a PEP 258 attribute docstring
            # following an assignment. The first version of this walk handled
            # only the first-statement kind and flagged _BUS_PREAMBLE's
            # attribute docstring -- which is where this codebase RECORDS the
            # retracted phrases -- as an emitted claim. The meta-test below
            # caught that, which is the only reason this comment exists.
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                docstrings.add(id(node.value))
        out: list[str] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                out.append(node.value)
        return out

    def test_no_emitted_string_makes_a_KNOWN_retracted_claim(self) -> None:
        # Derived, not a literal -- the same critique CONFINED_MODES' docstring
        # makes. codex_doctor.py was missing from the hand-written list, and at
        # one point emitted a false claim through that hole.
        import spanreed

        package_dir = Path(spanreed.__file__ or "").parent
        paths = sorted(package_dir.glob("*.py"))
        assert len(paths) >= 8, f"the package walk found only {len(paths)} modules"
        for path in paths:
            for text in self._emitted_strings(path):
                for phrase in self.FORBIDDEN:
                    assert phrase not in text, f"{path.name}: {phrase!r} in {text[:70]!r}"

    def test_the_scan_sees_emitted_strings_and_not_docstrings(self) -> None:
        # A guard that scans nothing passes everything. Prove both halves on a
        # module whose shape is known.
        import spanreed.codex_worker as worker_mod

        found = self._emitted_strings(Path(worker_mod.__file__ or ""))
        assert len(found) > 50, "the walk found almost nothing; it is not reading the module"
        assert any("REFUSING THE TURN" in t for t in found), "missed a real emitted string"
        assert not any(t.lstrip().startswith("Sent as ``developerInstructions``") for t in found), (
            "a docstring leaked into the emitted set"
        )


def test_no_prose_states_the_size_of_the_blacklist() -> None:
    """A count written in prose is a fact that drifts.

    FORBIDDEN grew by two entries while two docstrings and an open-questions
    entry went on stating the original count. That is the defect this whole
    guard exists to catch -- prose asserting something the code contradicts --
    inside the guard's own description of itself, which is the one place nobody
    thinks to check.

    Note that this docstring does not quote the stale figure either. It could
    be defended as a citation rather than a claim, and a guard that accepts
    "quoted counts are fine" has a hole shaped exactly like the thing it
    forbids. Describing it costs nothing.

    The fix is not to correct the number. It is to stop stating it: the list is
    right there and can be counted.

    **DECLARED LIMIT: this refuses a CONSTRUCT, not a claim.** It cannot
    distinguish an assertion from a citation, so it will also refuse a
    legitimate quotation of the old wording, or a sentence explaining that the
    count used to be wrong. That is the same defect shape as the doctor's step 3
    passing on its own prompt -- a predicate that cannot tell who is speaking.

    Kept blunt deliberately: the scope is the three files that describe this
    guard, where a citation of the old count is exactly the thing that drifts
    back into being a claim. If a fourth file ever needs to quote one, narrow
    the scope rather than adding a quote exemption -- an exemption would be a
    hole shaped like the thing it forbids.
    """
    import re

    from spanreed import codex_worker

    sources = {
        "test_codex_worker.py": Path(__file__).read_text(),
        "codex_worker.py": Path(codex_worker.__file__ or "").read_text(),
        "open-questions.md": (Path(__file__).parents[2] / "docs" / "open-questions.md").read_text(),
    }
    # Match a COUNT only -- a digit or a number word. The first version matched
    # any word before "phrase" against an allowlist of innocent ones, and
    # immediately flagged "retracted phrase" in this very file: a guard whose
    # own first run was a false positive is not one to keep.
    numbers = r"\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
    pattern = re.compile(rf"\b({numbers})[- ]phrase", re.I)
    for name, text in sources.items():
        match = pattern.search(text)
        assert match is None, (
            f"{name} states a blacklist size ({match.group(0)!r}); "
            f"FORBIDDEN has {len(TestNoBoundaryClaimInAnyEmittedString.FORBIDDEN)} "
            f"entries and can be counted"
        )
