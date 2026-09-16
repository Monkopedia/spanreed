"""A client for ``codex app-server``, the transport a Codex worker runs on.

Design: ``docs/architecture.md``, section "Codex workers". This module is the
protocol half of that design — spawn/connect, handshake, requests, streamed turn
events — and nothing above it: it knows nothing about inboxes, the registry, or
approval policy.

Everything here was established empirically by
``experiments/codex-app-server-spike/`` (thirty runs; its README is the record).
Four of those findings are load-bearing and are re-stated where the code
depends on them, because each one cost a day the first time:

1. **The child's output must be drained continuously, from the moment it is
   spawned.** The pipe holds 64KB; a Rust server at ``RUST_LOG=info`` fills it
   in milliseconds; when it fills, ``write()`` blocks *inside whatever request
   handler is logging* and the server never returns, never errors and never
   times out. See :func:`_start_reader`.
2. **The unix socket speaks WebSocket**, not newline-delimited JSON. Raw JSON
   gets parsed as an HTTP request line and the connection is dropped.
3. **The handshake is ``initialize`` then an ``initialized`` notification.**
   Anything sent before the notification is answered ``Not initialized``.
4. **Server→client requests must always be answered.** app-server asks the
   client questions mid-call (approvals, elicitation) and blocks forever on the
   answer, with no timeout. Unrecognised methods get ``-32601``: a refusal is
   diagnosable, a stall is not.
"""

from __future__ import annotations

import atexit
import base64
import contextlib
import hashlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any, cast

from spanreed import __version__

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
"""RFC 6455's fixed handshake GUID. Not a secret, not configurable."""

_OP_TEXT = 0x1
_OP_BINARY = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA

TERMINAL_TURN_EVENTS = frozenset({"turn/completed", "turn/failed", "turn/aborted"})
"""The notifications that end a turn. A turn that produced none of these has not
finished, however encouraging its ``turn/start`` response looked: ``turn/start``
returns ``status: "inProgress"`` on *acceptance*, which the spike once reported
as a completed turn (README, run 29)."""

PROHIBITED_THREAD_START_PARAMS = {
    # openai/codex#45361: on codex-cli 0.154.0 a per-thread `config` override
    # makes every subsequent turn hang silently — no notifications, no error, no
    # timeout. The API accepts it; we refuse to send it. Rejecting the caller is
    # the only failure mode here that is debuggable.
    "config": (
        "thread/start accepts `config` and must never be sent one: any override makes "
        "subsequent turns hang silently (openai/codex#45361). Configure the worker's "
        "CODEX_HOME instead."
    ),
    # `effort` is a turn/start parameter. thread/start takes it without
    # complaint and ignores it, so a worker-level effort set here would be
    # silently absent from every turn — see turn_start(), which re-sends it.
    "effort": (
        "thread/start silently ignores `effort`; it is a turn/start parameter. Pass it "
        "to turn_start() on every turn instead."
    ),
}
"""Parameters :meth:`CodexClient.thread_start` refuses, and why."""


class CodexError(RuntimeError):
    """A JSON-RPC error returned by app-server.

    Carries ``code`` so callers can tell expected refusals apart from faults.
    The one that matters in practice: ``thread/resume`` on a thread with no
    history answers ``-32600 no rollout found for thread id …``. That is the
    normal answer for a freshly created thread, not something to retry.
    """

    def __init__(self, method: str, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{method} failed: [{code}] {message}")
        self.method = method
        self.code = code
        self.message = message
        self.data = data


class CodexNotInitialized(RuntimeError):
    """Raised for a request made before the ``initialize``/``initialized`` handshake.

    app-server answers such a request ``Not initialized``; we refuse locally so
    the mistake names itself instead of arriving as a server error.
    """


NotificationHandler = Callable[[str, dict[str, Any]], None]
"""``(method, params)`` for every server→client notification seen while reading."""

ServerRequestHandler = Callable[[str, dict[str, Any]], dict[str, Any] | None]
"""``(method, params)`` for a server→client *request*.

Return the JSON-RPC ``result`` to send, or ``None`` to decline the method — the
client then answers ``-32601``. The handler is never allowed to decide *not* to
answer: an unanswered request hangs app-server forever.
"""


@dataclass
class TurnResult:
    """What :meth:`CodexClient.wait_for_turn` saw.

    ``completed`` is False when the deadline expired first — an accepted turn
    with no terminal event is a partial result, and the caller has to be able to
    tell the two apart.
    """

    completed: bool
    terminal: str | None = None
    # `default_factory=dict[str, Any]` rather than `dict`: the parameterised
    # alias is callable and carries the element types, which pyright --strict
    # needs to see.
    terminal_params: dict[str, Any] = field(default_factory=dict[str, Any])
    events: list[tuple[str, dict[str, Any]]] = field(
        default_factory=list[tuple[str, dict[str, Any]]]
    )


# --------------------------------------------------------------- WebSocket


ProtocolFaultHandler = Callable[[str], None]
"""``(detail)`` for a frame app-server sent that the protocol does not allow.

A fault is never silent and never fatal on its own: the frame is skipped, the
detail is appended to :meth:`CodexClient.server_log` and handed to this
callback if one is set. Silence here is the failure the spike paid for six
times over — a diagnostic that cannot report its own failure.
"""


def ws_handshake(sock: socket.socket, *, host: str = "localhost", path: str = "/") -> bytearray:
    """Upgrade ``sock`` to WebSocket. Returns body bytes that arrived with the 101.

    The unix socket is a *control socket* and expects an HTTP upgrade; sending
    JSON at it produces ``httparse error: invalid token`` and a closed
    connection, which reads exactly like a wrong-parameters failure.
    """
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(request.encode())
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("server closed during the WebSocket handshake")
        buf.extend(chunk)
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    headers = head.decode(errors="replace")
    status = headers.split("\r\n")[0]
    if "101" not in status:
        raise ConnectionError(f"no WebSocket upgrade: {status!r}")
    accept = base64.b64encode(hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
    if accept.lower() not in headers.lower():
        raise ConnectionError("Sec-WebSocket-Accept did not match — not a conformant upgrade")
    return bytearray(rest)


def ws_encode(payload: bytes, opcode: int = _OP_TEXT) -> bytes:
    """One client→server frame. Masking is mandatory in that direction: an
    unmasked client frame is a protocol error the server must close on."""
    mask = os.urandom(4)
    size = len(payload)
    if size < 126:
        header = struct.pack("!BB", 0x80 | opcode, 0x80 | size)
    elif size < 65536:
        header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, size)
    else:
        header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, size)
    return header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def ws_decode(buf: bytearray) -> tuple[int, bytes, int] | None:
    """Decode one frame from ``buf``, or ``None`` if more bytes are needed.

    Returns ``(opcode, payload, bytes_consumed)``. Handles masked frames too,
    so it decodes our own output in tests as well as the server's.
    """
    if len(buf) < 2:
        return None
    first, second = buf[0], buf[1]
    opcode, masked, size = first & 0x0F, second & 0x80, second & 0x7F
    offset = 2
    if size == 126:
        if len(buf) < 4:
            return None
        size = int(struct.unpack("!H", bytes(buf[2:4]))[0])
        offset = 4
    elif size == 127:
        if len(buf) < 10:
            return None
        size = int(struct.unpack("!Q", bytes(buf[2:10]))[0])
        offset = 10
    mask = b""
    if masked:
        if len(buf) < offset + 4:
            return None
        mask = bytes(buf[offset : offset + 4])
        offset += 4
    if len(buf) < offset + size:
        return None
    payload = bytes(buf[offset : offset + size])
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload, offset + size


# ------------------------------------------------------- spawned-child care


_SPAWNED: list[subprocess.Popen[str]] = []
"""Every app-server this process has spawned and not yet reaped.

A leaked server keeps contending for the same ``CODEX_HOME`` sqlite as the next
one, so the symptom of the leak shows up in the *next* run — the hardest kind of
failure to attribute (spike README, run 21).
"""

_reaper_installed = False


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _reap_all(*_: Any) -> None:
    for proc in list(_SPAWNED):
        _terminate(proc)


def _install_reaper() -> None:
    """Reap spawned servers on normal exit *and* on the signals that skip it.

    Installed on first spawn rather than at import, so importing this module
    never touches a process's signal disposition.
    """
    global _reaper_installed
    if _reaper_installed:
        return
    _reaper_installed = True
    atexit.register(_reap_all)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        previous = signal.getsignal(sig)

        def handler(
            signum: int, frame: FrameType | None, _previous: Any = previous
        ) -> None:  # pragma: no cover - signal delivery
            _reap_all()
            if callable(_previous):
                _previous(signum, frame)
            else:
                raise SystemExit(128 + signum)

        # signal.signal() is main-thread only; a worker embedded in a thread
        # still gets the atexit half.
        with contextlib.suppress(ValueError, OSError):
            _ = signal.signal(sig, handler)


class _OutputSink:
    """Bounded, thread-safe capture of a spawned server's output.

    Bounded because a worker lives for days and an INFO-level Rust log is not
    something to hold in memory forever; ``dropped`` is counted and reported so
    a truncated log says it is truncated rather than quietly lying about what
    the server did (rule 7).
    """

    def __init__(self, max_lines: int) -> None:
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._lock = threading.Lock()
        self.dropped = 0

    def append(self, line: str) -> None:
        with self._lock:
            if len(self._lines) == self._lines.maxlen:
                self.dropped += 1
            self._lines.append(line)

    def text(self) -> str:
        with self._lock:
            body = "".join(self._lines)
            dropped = self.dropped
        return f"[{dropped} earlier line(s) dropped]\n{body}" if dropped else body


def _start_reader(proc: subprocess.Popen[str], sink: _OutputSink) -> threading.Thread:
    """Drain the child's stdout/stderr continuously, on its own thread.

    **This is not optional and it is not a convenience.** The pipe is 64KB. A
    Rust server logging at INFO fills it in milliseconds, and a full pipe blocks
    the server's ``write()`` *inside the request handler that was logging* —
    which is a permanent hang with no error, no log line and no timeout, on
    every call that touches the backend. Thirty spike runs and eight wrong
    hypotheses came out of reading this pipe only at the end.
    """

    def pump() -> None:
        stream = proc.stdout
        if stream is None:  # pragma: no cover - we always spawn with a pipe
            return
        try:
            for line in stream:
                sink.append(line)
        except (ValueError, OSError):  # pragma: no cover - closed at shutdown
            pass

    thread = threading.Thread(target=pump, daemon=True, name=f"codex-drain-{proc.pid}")
    thread.start()
    return thread


# ------------------------------------------------------------------ client


class CodexClient:
    """One connection to one ``codex app-server``.

    Either spawns a private server on a socket in a temp dir (the default), or
    connects to ``socket_path`` if one is given. Not thread-safe: one turn at a
    time, which is what the FIFO worker design in ``docs/architecture.md``
    wants. The only other thread is the child's output drain.

    Usage::

        with CodexClient(on_server_request=approve) as codex:
            thread = codex.thread_start(cwd="/home/me/git/foo")
            codex.turn_start(thread["threadId"], "hello", effort="medium")
            result = codex.wait_for_turn()
    """

    def __init__(
        self,
        *,
        socket_path: str | Path | None = None,
        codex_cmd: Sequence[str] = ("codex", "app-server"),
        env: Mapping[str, str] | None = None,
        client_name: str = "spanreed",
        client_version: str = __version__,
        timeout: float = 90.0,
        turn_timeout: float = 600.0,
        spawn_timeout: float = 15.0,
        on_server_request: ServerRequestHandler | None = None,
        on_notification: NotificationHandler | None = None,
        on_protocol_error: ProtocolFaultHandler | None = None,
        log_lines: int = 20000,
    ) -> None:
        self._given_socket = Path(socket_path) if socket_path is not None else None
        self._codex_cmd = list(codex_cmd)
        self._env = dict(env) if env is not None else None
        self._client_info = {"name": client_name, "version": client_version}
        self.timeout = timeout
        self.turn_timeout = turn_timeout
        self.spawn_timeout = spawn_timeout
        self._on_server_request = on_server_request
        self._on_notification = on_notification
        self.on_protocol_error: ProtocolFaultHandler | None = on_protocol_error
        """Where protocol faults are reported, in addition to the captured log.

        A public attribute rather than a constructor-only argument so a caller
        that builds its client through a factory (the worker does) can still
        route faults into its own log without every factory growing a
        parameter.
        """

        self._proc: subprocess.Popen[str] | None = None
        # Why there is no process, for server_status(). "never attempted",
        # "attempted and failed", and "given a socket" are three different
        # diagnoses, and reporting the benign one unconditionally put a
        # confident wrong explanation under a real error.
        self._spawn_error: BaseException | None = None
        self._socket_path_given: bool = socket_path is not None
        self._tmpdir: Path | None = None
        self._sock: socket.socket | None = None
        self._buf = bytearray()
        self._next_id = 0
        self._initialized = False
        self._log = _OutputSink(log_lines)
        self._reader: threading.Thread | None = None
        self.socket_path: Path | None = self._given_socket
        self.server_info: dict[str, Any] = {}

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> CodexClient:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    @property
    def initialized(self) -> bool:
        """True once ``initialize`` was answered *and* ``initialized`` was sent."""
        return self._initialized

    @property
    def pid(self) -> int | None:
        """PID of the spawned server, or ``None`` when connected to someone else's."""
        return self._proc.pid if self._proc is not None else None

    def connect(self) -> dict[str, Any]:
        """Spawn (if needed), upgrade to WebSocket, and complete the handshake.

        Returns app-server's ``initialize`` result.
        """
        if self._sock is not None:
            raise RuntimeError("already connected")
        if self._given_socket is None:
            self._spawn()
        assert self.socket_path is not None
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(str(self.socket_path))
            self._buf = ws_handshake(sock)
        except OSError:
            sock.close()
            raise
        self._sock = sock

        # `capabilities.experimentalApi` is declared because app-server gates
        # methods behind it and is proven to accept it; `protocolVersion` and
        # `clientInfo` are the shape the server echoed back.
        result = self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"experimentalApi": True},
                "clientInfo": self._client_info,
            },
        )
        # The notification is mandatory and separate: `initialize` answering is
        # not the end of the handshake, and every method called before this one
        # is refused with `Not initialized`.
        self.notify("initialized", {})
        self._initialized = True
        self.server_info = result if isinstance(result, dict) else {}
        return self.server_info

    def close(self) -> None:
        """Close the connection and reap the spawned server, if any."""
        if self._sock is not None:
            with contextlib.suppress(OSError):
                self._sock.sendall(ws_encode(b"", _OP_CLOSE))
            with contextlib.suppress(OSError):
                self._sock.close()
            self._sock = None
        self._initialized = False
        if self._proc is not None:
            _terminate(self._proc)
            if self._proc in _SPAWNED:
                _SPAWNED.remove(self._proc)
            if self._reader is not None:
                # Bounded: the thread is a daemon, so a child that leaves the
                # pipe open to a grandchild must not hold up close().
                self._reader.join(timeout=1.0)
                self._reader = None
            self._proc = None
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None

    def server_status(self) -> str:
        """What became of the spawned server, in words. Never a bare code.

        Called on every failure path that has to explain itself to a reader who
        cannot attach a debugger: "the connection dropped" and "the process
        exited 101" are the same symptom and different diagnoses, and only one
        of them means restarting the worker will help.
        """
        if self._proc is None:
            # `_proc is None` has THREE causes and they are different diagnoses.
            # Reporting the benign one unconditionally printed "it connected to
            # an existing socket" directly beneath "FileNotFoundError: 'codex'",
            # which is a confident wrong explanation sitting under the real
            # error -- the exact defect this client was written to stop
            # producing.
            if self._spawn_error is not None:
                return (
                    f"no app-server is running: SPAWNING ONE FAILED with "
                    f"{type(self._spawn_error).__name__}: {self._spawn_error}. "
                    f"Nothing was started, so there is no server output below."
                )
            if self._socket_path_given:
                return "no app-server was spawned by this client; it used a socket it was given"
            return "no app-server was spawned by this client, and none was attempted"
        code = self._proc.poll()
        if code is None:
            return f"the app-server this client spawned is still running (pid {self._proc.pid})"
        return (
            f"the app-server this client spawned has EXITED with status {code} "
            f"(pid {self._proc.pid})"
        )

    def settle(self, timeout: float = 1.0) -> None:
        """Give a dying server the last word before its output is read.

        The drain runs on its own thread, so the line explaining why the server
        went away — which is the whole diagnosis — may not have been appended
        yet when a failure path reaches for :meth:`server_log`. Every caller
        here is already on the way to reporting a failure, so a bounded wait
        costs nothing and is the difference between a log that names the cause
        and one that stops just short of it.
        """
        proc = self._proc
        if proc is None:
            return
        try:
            _ = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return  # still running: nothing to flush, and not ours to kill here
        if self._reader is not None:
            self._reader.join(timeout=timeout)

    def _protocol_fault(self, detail: str) -> None:
        """Record one frame the protocol does not allow, and carry on.

        Skipping a bad frame is the recoverable answer — WebSocket frames are
        self-delimiting, so one unparseable frame does not desynchronise the
        stream — but a skip nobody is told about is how a worker ends up
        ingesting nothing while looking alive (issue #55). Both halves are
        required: the frame goes, the report stays.
        """
        self._log.append(f"[codex-client] PROTOCOL FAULT: {detail}\n")
        if self.on_protocol_error is not None:
            self.on_protocol_error(detail)

    def _params_of(self, msg: dict[str, Any], context: str) -> dict[str, Any]:
        """A message's ``params`` as a dict. Notifications may omit the member.

        A ``params`` that is present but is a string, a list or a number is a
        protocol violation, and it used to be handed to the caller's handler
        as-is: the worker then called ``.get()`` on a string and died with an
        ``AttributeError`` in the middle of a turn. It is reported and replaced
        with an empty object, which every handler here already treats as "the
        request named nothing", i.e. a decline.
        """
        params: Any = msg.get("params")
        if params is None:
            return {}
        if not isinstance(params, dict):
            method = msg.get("method") or "(no method)"
            self._protocol_fault(
                f"{method} arrived during {context} with a `params` member that is a "
                f"{type(params).__name__}, not a JSON object; it was replaced with an empty "
                f"object, so any decision made from it is a decline"
            )
            return {}
        return cast("dict[str, Any]", params)

    def _report_stray_response(self, msg: dict[str, Any], context: str) -> None:
        """A response we are not waiting for. Discarded — but never quietly.

        Two different faults wear this shape. An id inside the range this
        client has issued is a *late* answer, which means an earlier call
        timed out and the server answered it afterwards; an id outside that
        range was never ours at all, and points at id confusion on the wire.
        A reader needs to be told which one arrived.
        """
        rid = msg.get("id")
        if isinstance(rid, int) and 1 <= rid <= self._next_id:
            self._protocol_fault(
                f"a LATE response to request id {rid} arrived during {context} and was "
                f"discarded; the call that sent id {rid} had already given up waiting, so "
                f"the server is answering slower than this client's timeout"
            )
            return
        self._protocol_fault(
            f"a response for id {rid!r} arrived during {context} and was discarded: this "
            f"client has never sent that id (it has issued ids 1..{self._next_id}). Either "
            f"something else is writing to this socket or the server has confused two clients"
        )

    def server_log(self) -> str:
        """Everything the spawned server wrote, as captured by the drain thread.

        Prefixed with a notice when the ring buffer dropped lines, per rule 7:
        a truncated log that does not say so is how the spike spent five runs
        reading a constant as a measurement.
        """
        return self._log.text()

    def _spawn(self) -> None:
        self._tmpdir = Path(tempfile.mkdtemp(prefix="spanreed-codex-"))
        self.socket_path = self._tmpdir / "app.sock"
        cmd = [*self._codex_cmd, "--listen", f"unix://{self.socket_path}"]
        env = {**os.environ, **(self._env or {})}
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            # server_status() is read on the failure path and must not claim a
            # server exists, nor that none was attempted.
            self._spawn_error = exc
            raise
        self._proc = proc
        _SPAWNED.append(proc)
        _install_reaper()
        # Before anything else waits on this process, including the socket poll
        # below: an undrained pipe deadlocks the child at 64KB of output.
        self._reader = _start_reader(proc, self._log)

        deadline = time.monotonic() + self.spawn_timeout
        while time.monotonic() < deadline:
            if self.socket_path.exists():
                return
            if proc.poll() is not None:
                # Let the drain thread finish the tail first: the line naming
                # the reason is the whole diagnosis, and it arrives last.
                self._reader.join(timeout=1.0)
                raise RuntimeError(
                    f"{cmd[0]} exited {proc.returncode} before creating {self.socket_path}.\n"
                    f"{self.server_log()}"
                )
            time.sleep(0.05)
        raise TimeoutError(
            f"{cmd[0]} did not create {self.socket_path} within {self.spawn_timeout}s.\n"
            f"{self.server_log()}"
        )

    # -- JSON-RPC ----------------------------------------------------------

    def notify(self, method: str, params: Any = None) -> None:
        """Send a notification (no id, no reply)."""
        self._send(
            {"jsonrpc": "2.0", "method": method, "params": params if params is not None else {}}
        )

    def request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,
        on_notify: NotificationHandler | None = None,
    ) -> Any:
        """Send a request and return its result, servicing the stream meanwhile.

        Notifications go to ``on_notify`` (or the client-level handler) and
        server→client requests are answered as they arrive — both happen while
        this call is blocked, because the server may well be waiting on one of
        those answers before it can produce our result.
        """
        if not self._initialized:
            raise CodexNotInitialized(
                f"{method} was called before the initialize/initialized handshake; "
                "app-server refuses such requests with `Not initialized`"
            )
        return self._request(method, params, timeout=timeout, on_notify=on_notify)

    def _request(
        self,
        method: str,
        params: Any = None,
        *,
        timeout: float | None = None,
        on_notify: NotificationHandler | None = None,
    ) -> Any:
        self._next_id += 1
        mid = self._next_id
        self._send(
            {
                "jsonrpc": "2.0",
                "id": mid,
                "method": method,
                "params": params if params is not None else {},
            }
        )
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            msg = self._read_message(deadline, method)
            # A response, not a request: server→client requests carry a
            # `method` alongside their id, and the server's ids are numbered in
            # its own space, so they can collide with ours.
            if msg.get("id") == mid and "method" not in msg:
                error = msg.get("error")
                if error is not None:
                    raise CodexError(
                        method,
                        int(error.get("code", 0)),
                        str(error.get("message", "")),
                        error.get("data"),
                    )
                return msg.get("result")
            self._dispatch(msg, on_notify)

    def wait_for_turn(
        self,
        *,
        timeout: float | None = None,
        on_notify: NotificationHandler | None = None,
    ) -> TurnResult:
        """Consume the stream until a terminal turn event, or the deadline.

        ``turn/start`` returns as soon as the turn is *accepted*; the turn's
        output arrives afterwards as notifications. A deadline that expires
        first returns ``completed=False`` with whatever was seen rather than
        raising, so a partial turn stays distinguishable from a finished one.
        """
        deadline = time.monotonic() + (timeout if timeout is not None else self.turn_timeout)
        result = TurnResult(completed=False)
        while True:
            try:
                msg = self._read_message(deadline, "a turn")
            except TimeoutError:
                return result
            method = msg.get("method")
            if method is None:
                self._report_stray_response(msg, "a turn")
                continue
            if msg.get("id") is not None:
                self._answer_server_request(msg)
                continue
            params = self._params_of(msg, "a turn")
            result.events.append((str(method), params))
            handler = on_notify or self._on_notification
            if handler is not None:
                handler(method, params)
            if method in TERMINAL_TURN_EVENTS:
                result.completed = True
                result.terminal = method
                result.terminal_params = params
                return result

    # -- app-server methods ------------------------------------------------

    def thread_start(self, **params: Any) -> Any:
        """Create a thread this client owns.

        ``cwd`` is required — see below. Also takes ``model``, ``personality``,
        ``instructions`` and friends, and refuses the parameters in
        :data:`PROHIBITED_THREAD_START_PARAMS`. Names
        are not guessed — ``codex app-server generate-json-schema --out DIR``
        writes the authoritative set to disk.

        Driving a thread a *human* has open is not possible and is not attempted
        anywhere here: Codex holds an advisory ``flock`` on
        ``$CODEX_HOME/thread-writer-locks/<id>.lock`` for the life of that
        session. Owning our own thread is the supported path.
        """
        for name, why in PROHIBITED_THREAD_START_PARAMS.items():
            if name in params:
                raise ValueError(why)
        # `cwd` is the worker's whole security boundary and the doc gives it no
        # default — not $HOME, not the process cwd, not whatever config.toml
        # marks trusted. Inheriting one on the machine this was validated on
        # would have scoped a worker to the entire home directory, so the
        # omission is refused here too rather than only at the layer above.
        if not params.get("cwd"):
            raise ValueError(
                "thread_start requires an explicit cwd: it bounds everything the worker may "
                "touch, and app-server's default would be inherited from the environment"
            )
        return self.request("thread/start", params)

    def thread_resume(self, thread_id: str, *, exclude_turns: bool = True, **params: Any) -> Any:
        """Load an existing thread.

        ``exclude_turns`` defaults to True because paginated threads require it
        — a full-history resume is unavailable and plain resume hangs. A thread
        with no history answers ``-32600 no rollout found for thread id …``,
        which is the expected result for a thread just created, not a fault.
        """
        return self.request(
            "thread/resume", {"threadId": thread_id, "excludeTurns": exclude_turns, **params}
        )

    def thread_list(self, *, use_state_db_only: bool = True, **params: Any) -> Any:
        """List threads.

        ``useStateDbOnly`` defaults to True: the default path consults remote
        sources, and on a network where that lookup does not return, the call
        never answers (spike README, run 13).
        """
        return self.request("thread/list", {"useStateDbOnly": use_state_db_only, **params})

    def turn_start(self, thread_id: str, text: str, **params: Any) -> Any:
        """Start a turn with one text input.

        ``effort`` belongs here and only here: ``thread/start`` accepts it and
        ignores it, so a worker-level effort has to be re-sent on every turn.
        ``model`` and ``personality`` are accepted by both.
        """
        return self.request(
            "turn/start",
            {"threadId": thread_id, "input": [{"type": "text", "text": text}], **params},
        )

    # -- wire --------------------------------------------------------------

    def _send(self, msg: dict[str, Any]) -> None:
        if self._sock is None:
            raise RuntimeError("not connected")
        try:
            self._sock.sendall(ws_encode(json.dumps(msg).encode()))
        except OSError as exc:
            # A server that has already gone away fails the *write*, and the
            # bare `[Errno 32] Broken pipe` that comes out says nothing about
            # what was being sent or to whom. Callers branch on ConnectionError
            # (BrokenPipeError is one), so the type is preserved and only the
            # sentence improves.
            raise ConnectionError(
                f"the app-server connection was gone before "
                f"{msg.get('method') or 'a reply to a server request'} could be sent: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    def _read_message(self, deadline: float, context: str) -> dict[str, Any]:
        """Next JSON-RPC message off the wire. Ping/pong and control frames are
        handled here so no caller has to know they exist."""
        sock = self._sock
        if sock is None:
            raise RuntimeError("not connected")
        while True:
            decoded = ws_decode(self._buf)
            if decoded is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"no response to {context} before the deadline")
                sock.settimeout(remaining)
                try:
                    chunk = sock.recv(65536)
                except TimeoutError as exc:
                    raise TimeoutError(f"no response to {context} before the deadline") from exc
                if not chunk:
                    raise ConnectionError(f"server closed the connection during {context}")
                self._buf.extend(chunk)
                continue
            opcode, payload, used = decoded
            del self._buf[:used]
            if opcode == _OP_CLOSE:
                raise ConnectionError(f"server sent a WebSocket close frame during {context}")
            if opcode == _OP_PING:
                # A dropped ping gets the connection closed under us, and that
                # looks identical to the server refusing the call in progress.
                sock.sendall(ws_encode(payload, _OP_PONG))
                continue
            if opcode not in (_OP_TEXT, _OP_BINARY):
                continue
            try:
                decoded_msg: Any = json.loads(payload.decode(errors="replace"))
            except ValueError as exc:
                # One bad frame is not a broken stream: WebSocket frames carry
                # their own length, so the next frame still starts where it
                # should. Raising here instead killed the whole turn (and, for
                # a caller that did not expect a ValueError out of a read, the
                # whole worker) over a single unparseable notification.
                self._protocol_fault(
                    f"a frame that is not JSON arrived during {context} and was skipped: "
                    f"{exc}. First 200 bytes: {payload[:200]!r}"
                )
                continue
            if not isinstance(decoded_msg, dict):
                self._protocol_fault(
                    f"a JSON-RPC frame that is not an object (it is a "
                    f"{type(decoded_msg).__name__}) arrived during {context} and was skipped. "
                    f"First 200 bytes: {payload[:200]!r}"
                )
                continue
            # isinstance() narrows to dict[Unknown, Unknown]; the wire format
            # is JSON, so the keys are strings by construction.
            return cast("dict[str, Any]", decoded_msg)

    def _dispatch(self, msg: dict[str, Any], on_notify: NotificationHandler | None) -> None:
        method = msg.get("method")
        if method is None:
            self._report_stray_response(msg, "a request/response exchange")
            return
        if msg.get("id") is not None:
            self._answer_server_request(msg)
            return
        handler = on_notify or self._on_notification
        if handler is not None:
            handler(str(method), self._params_of(msg, "a request/response exchange"))

    def _answer_server_request(self, msg: dict[str, Any]) -> None:
        """Answer a server→client request. Always. Without exception.

        app-server blocks on these with no timeout, so a request we do not
        understand is answered ``-32601`` rather than dropped: at least four
        other clients shipped the drop-it bug and all four report the same
        symptom — a turn that hangs in ``waitingOnApproval`` forever.

        The reply deliberately omits the ``"jsonrpc": "2.0"`` member, matching
        the frames app-server sends itself.
        """
        rid = msg.get("id")
        method = str(msg.get("method") or "")
        params = self._params_of(msg, f"the server request {method}")
        reply: dict[str, Any] = {"id": rid}
        if self._on_server_request is None:
            reply["error"] = {
                "code": -32601,
                "message": f"this client has no handler for {method}",
            }
        else:
            try:
                result = self._on_server_request(method, params)
            except Exception as exc:
                # Even a broken handler gets an answer out: the alternative is
                # a server that waits on us forever.
                reply["error"] = {
                    "code": -32603,
                    "message": f"handler for {method} raised {type(exc).__name__}: {exc}",
                }
            else:
                if result is None:
                    reply["error"] = {
                        "code": -32601,
                        "message": f"this client does not implement {method}",
                    }
                else:
                    reply["result"] = result
        self._send(reply)
