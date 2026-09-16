"""Tests for the codex app-server client.

``codex`` is not installed on CI or on the machine this was written on, so
everything here runs against a stub: a stdlib RFC 6455 server on a unix socket
(``aiohttp`` is not a dependency of this project and adding one to get a test
stub would be worse than writing the fifty lines below).

Four of these tests exist because of a specific failure recorded in
``experiments/codex-app-server-spike/README.md``, and they are marked as such —
they are regression tests for bugs that each cost days: the undrained pipe, the
unanswered server request, the handshake ordering, and ``config`` on
``thread/start``.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from spanreed.codex_client import CodexClient, CodexError, CodexNotInitialized, ws_decode, ws_encode

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
"""RFC 6455's handshake GUID, spelled out here rather than imported from the
module under test: a stub that borrows the client's own constant cannot catch
the client getting it wrong."""

# ------------------------------------------------------------ stub server


def _server_handshake(conn: socket.socket) -> bytearray:
    """The server half of RFC 6455. Returns bytes that arrived after the headers."""
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            raise ConnectionError("client closed during the handshake")
        buf.extend(chunk)
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    key = ""
    for line in head.decode().split("\r\n"):
        if line.lower().startswith("sec-websocket-key:"):
            key = line.split(":", 1)[1].strip()
    accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
    conn.sendall(
        (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode()
    )
    return bytearray(rest)


def server_encode(payload: bytes, opcode: int = 0x1) -> bytes:
    """A server→client frame: same framing as the client's, never masked.

    Public because the fault tests put frames on the wire that JSON cannot
    express — a close frame mid-turn, bytes that are not JSON at all.
    """
    size = len(payload)
    if size < 126:
        header = struct.pack("!BB", 0x80 | opcode, size)
    elif size < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 126, size)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 127, size)
    return header + payload


Handler = Callable[["StubServer", dict[str, Any]], None]


class StubServer:
    """A stand-in for ``codex app-server`` on a unix socket.

    ``handler(stub, msg)`` is called for each client *request*; it decides what
    to send back (and may send notifications or server→client requests first).
    Client replies to our own requests land in :attr:`client_replies`, which is
    how the "always answer a server request" tests observe the answer.
    """

    def __init__(self, path: Path, handler: Handler | None = None) -> None:
        self.path = path
        self.handler: Handler = handler if handler is not None else _default_handler
        self.received: list[dict[str, Any]] = []
        self.client_replies: list[dict[str, Any]] = []
        self.conn: socket.socket | None = None
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(str(path))
        self._listener.listen(1)
        self._thread = threading.Thread(target=self._serve, daemon=True, name="stub-app-server")
        self._thread.start()

    @property
    def methods(self) -> list[str]:
        """Every method name the client sent, in order (requests and notifications)."""
        return [str(m["method"]) for m in self.received if "method" in m]

    def send(self, obj: dict[str, Any]) -> None:
        assert self.conn is not None
        self.conn.sendall(server_encode(json.dumps(obj).encode()))

    def reply(self, mid: Any, result: Any = None, error: Any = None) -> None:
        msg: dict[str, Any] = {"id": mid}
        if error is not None:
            msg["error"] = error
        else:
            msg["result"] = result
        self.send(msg)

    def notify(self, method: str, params: Any = None) -> None:
        self.send({"method": method, "params": params or {}})

    def ask(self, rid: Any, method: str, params: Any = None) -> None:
        """Send a server→client *request* — the kind that hangs an unprepared client."""
        self.send({"id": rid, "method": method, "params": params or {}})

    def wait_for(self, predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._listener.close()
        if self.conn is not None:
            with contextlib.suppress(OSError):
                self.conn.close()

    def _serve(self) -> None:
        try:
            conn, _ = self._listener.accept()
        except OSError:
            return
        self.conn = conn
        try:
            buf = _server_handshake(conn)
            while True:
                decoded = ws_decode(buf)
                if decoded is None:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    buf.extend(chunk)
                    continue
                opcode, payload, used = decoded
                del buf[:used]
                if opcode == 0x8:
                    return
                if opcode not in (0x1, 0x2):
                    continue
                msg = json.loads(payload.decode())
                self.received.append(msg)
                if "method" not in msg:
                    self.client_replies.append(msg)
                elif "id" in msg:
                    # On its own thread: a handler that sends a server→client
                    # request has to wait for the client's answer, and this loop
                    # is what delivers it. Running the handler inline would
                    # deadlock the stub, not the client under test.
                    threading.Thread(
                        target=self.handler, args=(self, msg), daemon=True, name="stub-handler"
                    ).start()
        except (OSError, ConnectionError, json.JSONDecodeError):
            return


def _approve(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"decision": "approve"}


def _default_handler(stub: StubServer, msg: dict[str, Any]) -> None:
    method = msg.get("method")
    if method == "initialize":
        stub.reply(msg["id"], {"serverInfo": {"name": "stub-app-server", "version": "0"}})
    else:
        stub.reply(msg["id"], {"ok": method})


@pytest.fixture
def stub_factory(tmp_path: Path) -> Iterator[Callable[[Handler | None], StubServer]]:
    stubs: list[StubServer] = []
    counter = [0]

    def make(handler: Handler | None = None) -> StubServer:
        counter[0] += 1
        stub = StubServer(tmp_path / f"stub-{counter[0]}.sock", handler)
        stubs.append(stub)
        return stub

    yield make
    for stub in stubs:
        stub.close()


@pytest.fixture
def connected(
    stub_factory: Callable[[Handler | None], StubServer],
) -> Iterator[Callable[..., tuple[StubServer, CodexClient]]]:
    clients: list[CodexClient] = []

    def make(handler: Handler | None = None, **kwargs: Any) -> tuple[StubServer, CodexClient]:
        stub = stub_factory(handler)
        client = CodexClient(socket_path=stub.path, timeout=5.0, **kwargs)
        clients.append(client)
        client.connect()
        return stub, client

    yield make
    for client in clients:
        client.close()


# ------------------------------------------------------------- ws framing


class TestFraming:
    """The three RFC 6455 length encodings, round-tripped.

    A frame longer than 125 bytes uses a 2-byte length and one longer than
    65535 an 8-byte one; every real payload here (a turn's text, a patch) is in
    the second or third bucket, so getting only the first right would look fine
    against a hello-world and fail on the first real turn.
    """

    @pytest.mark.parametrize("size", [0, 5, 125, 126, 1000, 65535, 65536, 200000])
    def test_roundtrip(self, size: int) -> None:
        payload = os.urandom(size)
        frame = ws_encode(payload)
        decoded = ws_decode(bytearray(frame))
        assert decoded is not None
        opcode, got, used = decoded
        assert opcode == 0x1
        assert got == payload
        assert used == len(frame)

    @pytest.mark.parametrize(
        ("size", "indicator", "header_len"),
        [(125, 125, 2), (126, 126, 4), (65535, 126, 4), (65536, 127, 10)],
    )
    def test_length_encoding(self, size: int, indicator: int, header_len: int) -> None:
        frame = ws_encode(b"x" * size)
        assert frame[1] & 0x7F == indicator
        assert frame[1] & 0x80, "client frames must be masked"
        assert len(frame) == header_len + 4 + size  # header + mask key + payload

    def test_partial_frame_needs_more_bytes(self) -> None:
        frame = ws_encode(b"y" * 300)
        for cut in (1, 3, 7, len(frame) - 1):
            assert ws_decode(bytearray(frame[:cut])) is None

    def test_two_frames_in_one_buffer(self) -> None:
        buf = bytearray(ws_encode(b"first") + ws_encode(b"second"))
        first = ws_decode(buf)
        assert first is not None
        del buf[: first[2]]
        second = ws_decode(buf)
        assert second is not None
        assert (first[1], second[1]) == (b"first", b"second")


# ------------------------------------------------------------- handshake


class TestHandshake:
    def test_initialize_then_initialized_before_anything_else(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """Regression (spike README, run 13): nine runs hung because `initialized`
        was never sent. app-server answers `initialize`, so the connection looks
        established, and every later call is refused with `Not initialized`."""
        stub, client = connected()
        client.thread_list()
        assert stub.methods == ["initialize", "initialized", "thread/list"]

    def test_initialize_declares_experimental_api(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        stub, _client = connected()
        params = stub.received[0]["params"]
        assert params["capabilities"] == {"experimentalApi": True}
        assert params["clientInfo"]["name"] == "spanreed"

    def test_initialized_is_a_notification(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        stub, _client = connected()
        # The notification needs no reply, so nothing else forces it to arrive
        # before the assertion; wait for it rather than race it.
        assert stub.wait_for(lambda: len(stub.received) >= 2)
        sent = next(m for m in stub.received if m.get("method") == "initialized")
        assert "id" not in sent, "a notification must carry no id, or the server waits to reply"

    def test_request_before_handshake_is_refused_locally(self, tmp_path: Path) -> None:
        client = CodexClient(socket_path=tmp_path / "never.sock")
        assert not client.initialized
        with pytest.raises(CodexNotInitialized):
            client.request("thread/list", {})

    def test_server_info_is_exposed(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        _stub, client = connected()
        assert client.server_info["serverInfo"]["name"] == "stub-app-server"
        assert client.initialized


# ------------------------------------------------- server→client requests


class TestServerRequests:
    """app-server asks the client questions mid-call and blocks on the answer
    forever, with no timeout. Every one of these tests asserts that *something*
    went back; silence is the failure being guarded against."""

    @staticmethod
    def _ask_then_reply(method: str) -> Handler:
        def handler(stub: StubServer, msg: dict[str, Any]) -> None:
            if msg.get("method") == "initialize":
                _default_handler(stub, msg)
                return
            stub.ask("srv-1", method, {"command": ["rm", "-rf", "/"]})
            if not stub.wait_for(lambda: bool(stub.client_replies)):
                raise AssertionError("client never answered the server request")
            stub.reply(msg["id"], {"ok": True})

        return handler

    def test_unknown_method_gets_method_not_found(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """Regression: an unrecognised server request must be REFUSED, not dropped.
        At least four third-party clients shipped the drop-it bug; the symptom is
        a turn stuck in `waitingOnApproval` with no error and no timeout."""
        stub, client = connected(self._ask_then_reply("someMethod/nobodyImplements"))
        assert client.thread_list() == {"ok": True}
        (reply,) = stub.client_replies
        assert reply["id"] == "srv-1"
        assert reply["error"]["code"] == -32601

    def test_reply_omits_the_jsonrpc_member(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """app-server leaves `jsonrpc` off its own frames; we match it."""
        stub, client = connected(self._ask_then_reply("someMethod/nobodyImplements"))
        client.thread_list()
        assert "jsonrpc" not in stub.client_replies[0]

    def test_handler_result_is_sent(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        seen: list[str] = []

        def approve(method: str, params: dict[str, Any]) -> dict[str, Any]:
            seen.append(method)
            return {"decision": "approve"}

        stub, client = connected(
            self._ask_then_reply("execCommandApproval"), on_server_request=approve
        )
        client.thread_list()
        assert seen == ["execCommandApproval"]
        assert stub.client_replies[0]["result"] == {"decision": "approve"}

    def test_handler_returning_none_declines(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        def unhandled(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
            return None

        stub, client = connected(
            self._ask_then_reply("mcpServer/elicitation/request"), on_server_request=unhandled
        )
        client.thread_list()
        assert stub.client_replies[0]["error"]["code"] == -32601

    def test_handler_that_raises_still_answers(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """A broken handler must not become a hung server."""

        def boom(method: str, params: dict[str, Any]) -> dict[str, Any]:
            raise RuntimeError("handler is broken")

        stub, client = connected(self._ask_then_reply("applyPatchApproval"), on_server_request=boom)
        client.thread_list()
        assert stub.client_replies[0]["error"]["code"] == -32603
        assert "handler is broken" in stub.client_replies[0]["error"]["message"]

    def test_server_request_during_a_turn_is_answered(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """The same guarantee while streaming, not only while awaiting a result."""

        def handler(stub: StubServer, msg: dict[str, Any]) -> None:
            if msg.get("method") == "initialize":
                _default_handler(stub, msg)
                return
            stub.reply(msg["id"], {"status": "inProgress"})
            stub.ask("srv-turn", "execCommandApproval", {"command": ["ls"]})
            stub.wait_for(lambda: bool(stub.client_replies))
            stub.notify("turn/completed", {"usage": {}})

        stub, client = connected(handler, on_server_request=_approve)
        client.turn_start("t-1", "hello")
        result = client.wait_for_turn(timeout=5.0)
        assert result.completed
        assert stub.client_replies[0]["result"] == {"decision": "approve"}

    def test_server_request_id_may_collide_with_ours(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """The server numbers its requests in its own space, so a server request
        can carry the id we are waiting on. It is a request (it has a `method`),
        and must not be mistaken for our response."""

        def handler(stub: StubServer, msg: dict[str, Any]) -> None:
            if msg.get("method") == "initialize":
                _default_handler(stub, msg)
                return
            stub.ask(msg["id"], "execCommandApproval", {"command": ["ls"]})
            stub.wait_for(lambda: bool(stub.client_replies))
            stub.reply(msg["id"], {"threads": []})

        _stub, client = connected(handler, on_server_request=_approve)
        assert client.thread_list() == {"threads": []}


# ---------------------------------------------------------- app-server API


class TestMethods:
    def test_config_on_thread_start_raises(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """Regression (openai/codex#45361): a per-thread `config` override makes
        every later turn hang silently. The API accepts it; we refuse to send
        it, because the alternative failure is invisible."""
        stub, client = connected()
        with pytest.raises(ValueError, match="45361"):
            client.thread_start(cwd="/tmp", config={"model_reasoning_effort": "high"})
        assert "thread/start" not in stub.methods, "the prohibited call must not reach the wire"

    def test_effort_on_thread_start_raises(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """`effort` is a turn/start parameter. thread/start takes it and ignores
        it, so accepting it here would silently drop the worker's effort setting."""
        _stub, client = connected()
        with pytest.raises(ValueError, match="turn_start"):
            client.thread_start(cwd="/tmp", effort="medium")

    def test_thread_start_without_cwd_raises(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """`--cwd` is the worker's only blast-radius bound and the design doc gives
        it no default; an inherited one would scope a worker to the whole home
        directory."""
        stub, client = connected()
        with pytest.raises(ValueError, match="explicit cwd"):
            client.thread_start(model="gpt-5.6-sol")
        assert "thread/start" not in stub.methods

    def test_thread_start_passes_params_through(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        stub, client = connected()
        client.thread_start(cwd="/home/me/git/foo", model="gpt-5.6-sol", personality="concise")
        sent = next(m for m in stub.received if m.get("method") == "thread/start")
        assert sent["params"] == {
            "cwd": "/home/me/git/foo",
            "model": "gpt-5.6-sol",
            "personality": "concise",
        }

    def test_thread_list_defaults_to_the_local_db(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """Without `useStateDbOnly` the call consults remote sources and, on a
        network where that lookup stalls, never returns (spike README, run 13)."""
        stub, client = connected()
        client.thread_list()
        sent = next(m for m in stub.received if m.get("method") == "thread/list")
        assert sent["params"] == {"useStateDbOnly": True}

    def test_thread_resume_defaults_to_excluding_turns(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        stub, client = connected()
        client.thread_resume("t-7")
        sent = next(m for m in stub.received if m.get("method") == "thread/resume")
        assert sent["params"] == {"threadId": "t-7", "excludeTurns": True}

    def test_thread_resume_no_rollout_is_a_clean_error(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """The expected answer for a thread that has no history yet. It arrives as
        a normal JSON-RPC error, carrying a code the caller can branch on."""

        def handler(stub: StubServer, msg: dict[str, Any]) -> None:
            if msg.get("method") == "initialize":
                _default_handler(stub, msg)
                return
            stub.reply(
                msg["id"], error={"code": -32600, "message": "no rollout found for thread id t-7"}
            )

        _stub, client = connected(handler)
        with pytest.raises(CodexError) as excinfo:
            client.thread_resume("t-7")
        assert excinfo.value.code == -32600
        assert "no rollout found" in excinfo.value.message

    def test_turn_start_shape_and_per_turn_effort(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """`effort` rides on every turn because thread/start cannot hold it."""
        stub, client = connected()
        client.turn_start("t-1", "review the diff", effort="medium", model="gpt-5.6-sol")
        sent = next(m for m in stub.received if m.get("method") == "turn/start")
        assert sent["params"] == {
            "threadId": "t-1",
            "input": [{"type": "text", "text": "review the diff"}],
            "effort": "medium",
            "model": "gpt-5.6-sol",
        }


def _record(sink: list[str]) -> Callable[[str, dict[str, Any]], None]:
    def handler(method: str, params: dict[str, Any]) -> None:
        sink.append(method)

    return handler


class TestTurnStream:
    @staticmethod
    def _streaming(*events: tuple[str, dict[str, Any]]) -> Handler:
        def handler(stub: StubServer, msg: dict[str, Any]) -> None:
            if msg.get("method") == "initialize":
                _default_handler(stub, msg)
                return
            stub.reply(msg["id"], {"status": "inProgress"})
            for method, params in events:
                stub.notify(method, params)

        return handler

    def test_reads_until_a_terminal_event(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        seen: list[str] = []
        stub, client = connected(
            self._streaming(
                ("item/agentMessage/delta", {"delta": "hel"}),
                ("item/agentMessage/delta", {"delta": "lo"}),
                ("account/rateLimits/updated", {"remaining": 42}),
                ("turn/completed", {"usage": {"input": 1}}),
            ),
            on_notification=_record(seen),
        )
        assert client.turn_start("t-1", "hi") == {"status": "inProgress"}
        result = client.wait_for_turn(timeout=5.0)
        assert result.completed
        assert result.terminal == "turn/completed"
        assert result.terminal_params == {"usage": {"input": 1}}
        assert [m for m, _ in result.events] == [
            "item/agentMessage/delta",
            "item/agentMessage/delta",
            "account/rateLimits/updated",
            "turn/completed",
        ]
        assert seen == [m for m, _ in result.events]
        assert stub.methods[-1] == "turn/start"

    @pytest.mark.parametrize("terminal", ["turn/failed", "turn/aborted"])
    def test_failure_and_abort_are_terminal_too(
        self, connected: Callable[..., tuple[StubServer, CodexClient]], terminal: str
    ) -> None:
        _stub, client = connected(self._streaming((terminal, {"error": "boom"})))
        client.turn_start("t-1", "hi")
        result = client.wait_for_turn(timeout=5.0)
        assert result.completed and result.terminal == terminal

    def test_no_terminal_event_is_a_partial_result_not_a_completed_turn(
        self, connected: Callable[..., tuple[StubServer, CodexClient]]
    ) -> None:
        """Run 29 of the spike called an accepted turn a completed one. Accepted
        and completed are different claims; a caller must be able to tell."""
        _stub, client = connected(self._streaming(("turn/started", {})))
        client.turn_start("t-1", "hi")
        result = client.wait_for_turn(timeout=0.4)
        assert not result.completed
        assert result.terminal is None
        assert [m for m, _ in result.events] == ["turn/started"]


# --------------------------------------------------------- spawned server


def _write_noise(total: int) -> None:
    """Write ``total`` bytes to stdout, the way a Rust server at INFO does."""
    written = 0
    line = "x" * 99 + "\n"
    while written < total:
        sys.stdout.write(line)
        written += len(line)
    sys.stdout.flush()


CHILD_THREAD_ID = "t-child-1"
"""The thread id the spawned child hands out, so a worker test can name it."""


def run_stub_child(argv: list[str]) -> None:
    """Entry point for the spawned-child tests. Runs in a subprocess.

    Writes ``STUB_NOISE_BYTES`` of output *before* binding its socket, which is
    how the pipe-drain regression test puts the child in the exact position a
    real app-server is in: producing log faster than a client that is not
    reading it can absorb.

    The other environment knobs exist for the fault-injection tests, and each
    one is a failure a real app-server can produce:

    ``STUB_NEVER_BIND``
        Seconds to stay alive without ever creating the socket. A server that
        starts, logs, and never listens.
    ``STUB_TURN_NOISE_BYTES``
        Bytes written to stdout *after accepting ``turn/start`` and before the
        terminal event* — the 64KB pipe filling in the middle of a turn rather
        than before the socket exists.
    ``STUB_DIE_AFTER``
        ``handshake`` exits 0 once ``initialize`` is answered; ``turn`` SIGKILLs
        itself once ``turn/start`` is accepted, with no terminal event and no
        close frame.
    """
    listen = next(a for a in argv if a.startswith("unix://"))
    path = listen[len("unix://") :]
    _write_noise(int(os.environ.get("STUB_NOISE_BYTES", "0")))

    never_bind = os.environ.get("STUB_NEVER_BIND")
    if never_bind:
        sys.stdout.write("stub-child: alive, logging, and never binding a socket\n")
        sys.stdout.flush()
        time.sleep(float(never_bind))
        return

    die_after = os.environ.get("STUB_DIE_AFTER", "")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(1)
    conn, _ = listener.accept()
    buf = _server_handshake(conn)

    def send(obj: dict[str, Any]) -> None:
        conn.sendall(server_encode(json.dumps(obj).encode()))

    while True:
        decoded = ws_decode(buf)
        if decoded is None:
            chunk = conn.recv(65536)
            if not chunk:
                return
            buf.extend(chunk)
            continue
        opcode, payload, used = decoded
        del buf[:used]
        if opcode == 0x8:
            return
        if opcode not in (0x1, 0x2):
            continue
        msg = json.loads(payload.decode())
        method = msg.get("method")
        if method == "initialize":
            send({"id": msg["id"], "result": {"serverInfo": {"name": "noisy"}}})
            if die_after == "handshake":
                sys.stdout.write("stub-child: exiting 0 right after the handshake\n")
                sys.stdout.flush()
                return
        elif method == "thread/start":
            send({"id": msg["id"], "result": {"threadId": CHILD_THREAD_ID}})
        elif method == "turn/start":
            send({"id": msg["id"], "result": {"status": "inProgress"}})
            _write_noise(int(os.environ.get("STUB_TURN_NOISE_BYTES", "0")))
            if die_after == "turn":
                # SIGKILL, not exit(): a server that is killed mid-turn gets no
                # chance to close the socket politely, and the client has to
                # cope with the abrupt half of that pair too.
                os.kill(os.getpid(), signal.SIGKILL)
            send({"method": "item/agentMessage/delta", "params": {"delta": "child replied"}})
            send({"method": "turn/completed", "params": {"usage": {}}})
        elif "id" in msg:
            send({"id": msg["id"], "result": {"ok": method}})


_CHILD_BOOTSTRAP = (
    "import sys; sys.path.insert(0, {root!r}); "
    "from tests.unit.test_codex_client import run_stub_child; run_stub_child(sys.argv)"
)

NOISE_BYTES = 300_000
"""Comfortably past the 64KB pipe. An earlier attempt at this test wrote 28KB,
stayed under the buffer, passed against both the broken and the fixed client,
and proved nothing (spike README, "Demonstrated, not argued")."""


def child_cmd() -> list[str]:
    """The argv that runs :func:`run_stub_child` as a real child process."""
    root = str(Path(__file__).resolve().parents[2])
    return [sys.executable, "-c", _CHILD_BOOTSTRAP.format(root=root)]


class TestSpawn:
    def test_child_output_over_64k_does_not_deadlock(self) -> None:
        """Regression, and the most expensive bug in this protocol's history:
        the child's pipe holds 64KB, and a server that fills it blocks inside
        the handler that was logging — forever, with no error and no timeout.

        The child here writes 300KB *before* it binds its socket, so a client
        that does not drain continuously never sees the socket appear at all.
        """
        client = CodexClient(
            codex_cmd=child_cmd(),
            env={"STUB_NOISE_BYTES": str(NOISE_BYTES)},
            timeout=10.0,
            spawn_timeout=20.0,
        )
        try:
            info = client.connect()
            assert info["serverInfo"]["name"] == "noisy"
            log = client.server_log()
            # Not "we got some output": the whole of it, untruncated. A partial
            # capture is what a 64KB-limited read looks like.
            assert len(log) >= NOISE_BYTES, f"captured only {len(log)} of {NOISE_BYTES} bytes"
            assert not log.startswith("["), "the ring buffer should not have dropped lines here"
        finally:
            client.close()

    def test_a_child_that_dies_reports_its_output(self, tmp_path: Path) -> None:
        """A server that exits explaining itself must not have the explanation
        thrown away — that line is the whole diagnosis."""
        script = "import sys; sys.stderr.write('no such subcommand\\n'); sys.exit(2)"
        client = CodexClient(codex_cmd=[sys.executable, "-c", script], spawn_timeout=10.0)
        try:
            with pytest.raises(RuntimeError, match="no such subcommand"):
                client.connect()
        finally:
            client.close()

    def test_close_reaps_the_child(self) -> None:
        client = CodexClient(
            codex_cmd=child_cmd(), env={"STUB_NOISE_BYTES": "0"}, spawn_timeout=20.0
        )
        client.connect()
        pid = client.pid
        assert pid is not None
        client.close()
        assert not _pid_alive(pid)

    def test_socket_lives_in_a_private_temp_dir(self) -> None:
        """The worker gets its own socket rather than joining a shared one: a
        foreign client cannot drive threads another process holds anyway."""
        client = CodexClient(
            codex_cmd=child_cmd(), env={"STUB_NOISE_BYTES": "0"}, spawn_timeout=20.0
        )
        client.connect()
        path = client.socket_path
        assert path is not None and path.exists()
        client.close()
        assert not path.exists(), "the temp dir should go with the client"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - not reachable for our own child
        return True
    # A reaped child of ours is a zombie until waited on, and a zombie answers
    # signal 0. Ask the OS what state it is in.
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return bool(state.stdout.strip()) and not state.stdout.strip().startswith("Z")


class TestServerStatusTellsTheTruth:
    """`_proc is None` has three causes and they are different diagnoses.

    Reporting the benign one unconditionally printed "it connected to an
    existing socket" directly beneath "FileNotFoundError: 'codex'" -- a
    confident wrong explanation sitting under the real error, which is the
    defect this whole module was written to stop producing. Found by running
    the README's own commands (repo rule 6).
    """

    def test_a_failed_spawn_says_the_spawn_failed(self) -> None:
        client = CodexClient(codex_cmd=("definitely-not-a-real-binary-xyzzy",))
        with pytest.raises(OSError):
            client.connect()
        status = client.server_status()
        assert "SPAWNING ONE FAILED" in status
        # The exact wrong sentence must not come back.
        assert "connected to an existing socket" not in status
        assert "no server output below" in status

    def test_a_given_socket_says_it_was_given(self, tmp_path: Path) -> None:
        client = CodexClient(socket_path=tmp_path / "app.sock")
        assert "used a socket it was given" in client.server_status()

    def test_an_untouched_client_claims_nothing(self) -> None:
        client = CodexClient()
        status = client.server_status()
        assert "none was attempted" in status
        assert "FAILED" not in status
