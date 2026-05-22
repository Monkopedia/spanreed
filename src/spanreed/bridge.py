"""Cross-host bus-bridge: connect two local Spanreed buses over a duplex pipe.

Design and wire-format live in ``docs/architecture.md`` and ``docs/protocol.md``.
In brief: a symmetric bridge process runs on each host, connected by one
persistent duplex pipe (``spanreed conjoin <host>`` spawns ``spanreed conjoin
--serve`` on the peer over SSH; both then run :class:`Bridge`). Each side:

- forwards messages local agents addressed to ``*@<peer>`` over the pipe,
- delivers messages arriving over the pipe into local inboxes,
- mirrors the peer's live agents into the local registry (as ``<id>@<peer>``).

Status: prototype. Point-to-point only. ``conjoin`` reconnects with backoff
when the pipe dies (foreground command — supervision is the user's job);
multi-hop routing and peer-host discovery are out of scope for now.
"""

from __future__ import annotations

import json
import os
import random
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from typing import IO

from spanreed.protocol import Agent, Message
from spanreed.store import StateStore, pid_start_time


def _qualify_from(from_agent: str, self_host: str) -> str:
    """Qualify a bare local sender id with this host, for the receiver's view."""
    return from_agent if "@" in from_agent else f"{from_agent}@{self_host}"


def _strip_peer(to_agent: str, peer_host: str) -> str:
    """Turn ``agent-X@<peer>`` back into the bare ``agent-X`` local to the peer."""
    suffix = f"@{peer_host}"
    return to_agent[: -len(suffix)] if to_agent.endswith(suffix) else to_agent


class Bridge:
    """One end of a cross-host bridge. Symmetric — both ends run this."""

    def __init__(
        self,
        read: IO[bytes],
        write: IO[bytes],
        self_host: str,
        store: StateStore,
        *,
        poll_interval: float = 0.5,
        sync_interval: float = 3.0,
        recv_timeout: float | None = None,
    ) -> None:
        self._read = read
        self._write = write
        self.self_host = self_host
        self.store = store
        self.poll_interval = poll_interval
        self.sync_interval = sync_interval
        # Watchdog: if no frame (incl. the peer's pings) arrives within this
        # window, treat the pipe as dead even if it hasn't surfaced EOF.
        self.recv_timeout = recv_timeout if recv_timeout is not None else sync_interval * 5
        self.peer_host: str | None = None
        self._stop = threading.Event()
        self._hello = threading.Event()
        self._last_recv = time.monotonic()
        self._my_pid = os.getpid()
        self._my_pid_start = pid_start_time(self._my_pid)

    # --- framing (only the main thread writes, so no lock needed) ---

    def _send(self, frame: dict[str, object]) -> bool:
        """Write a frame to the pipe. Returns False (and stops) on a dead pipe."""
        try:
            self._write.write((json.dumps(frame) + "\n").encode())
            self._write.flush()
            return True
        except (BrokenPipeError, ValueError):
            self._stop.set()
            return False

    def stop(self) -> None:
        """Signal the bridge to shut down (cleared on the next loop turn)."""
        self._stop.set()

    def _cursor_key(self, inbox_id: str) -> str:
        return f".bridge.{inbox_id}"

    # --- reader thread ---

    def _reader(self) -> None:
        for raw in self._read:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
                kind = frame.get("kind")
            except (json.JSONDecodeError, AttributeError):
                continue
            self._last_recv = time.monotonic()  # any frame proves the pipe is alive
            if kind == "hello":
                self.peer_host = str(frame["host"])
                self._hello.set()
            elif kind == "msg":
                self.store.append_message(Message.model_validate(frame["message"]))
            elif kind == "registry":
                if self.peer_host is not None:
                    agents = [Agent.model_validate(a) for a in frame["agents"]]
                    self.store.sync_remote_agents(
                        self.peer_host, agents, self._my_pid, self._my_pid_start
                    )
            # "ping" needs no action — it just keeps the pipe warm.
        self._stop.set()  # EOF: peer/pipe gone.

    # --- outbound + periodic sync (main thread) ---

    def _forward_outbound(self) -> None:
        assert self.peer_host is not None
        inboxes_dir = self.store.root / "inboxes"
        for inbox_file in sorted(inboxes_dir.glob(f"*@{self.peer_host}.jsonl")):
            inbox_id = inbox_file.stem
            cursor = self.store.get_cursor(self._cursor_key(inbox_id))
            for msg in self.store.recv_messages(inbox_id, since_msg_id=cursor):
                rewritten = Message(
                    msg_id=msg.msg_id,
                    from_agent=_qualify_from(msg.from_agent, self.self_host),
                    to_agent=_strip_peer(msg.to_agent, self.peer_host),
                    body=msg.body,
                    ts=msg.ts,
                    in_reply_to=msg.in_reply_to,
                )
                # Advance the cursor only after a confirmed send, so a message
                # in flight when the pipe dies is re-sent (not lost) on
                # reconnect. Receivers dedupe by msg_id.
                if not self._send({"kind": "msg", "message": rewritten.model_dump(mode="json")}):
                    return
                self.store.set_cursor(self._cursor_key(inbox_id), msg.msg_id)

    def _send_registry(self) -> None:
        local = [a for a in self.store.list_agents() if "@" not in a.agent_id]
        self._send({"kind": "registry", "agents": [a.model_dump(mode="json") for a in local]})

    def run(self) -> float:
        """Run until the pipe dies or :meth:`stop` is called. Returns uptime (s)."""
        started = time.monotonic()
        self._send({"kind": "hello", "host": self.self_host})
        reader = threading.Thread(target=self._reader, daemon=True)
        reader.start()
        if not self._hello.wait(timeout=15.0):
            self._stop.set()
            return time.monotonic() - started
        self._last_recv = time.monotonic()
        self._send_registry()
        last_sync = time.monotonic()
        try:
            while not self._stop.is_set():
                self._forward_outbound()
                now = time.monotonic()
                if now - last_sync >= self.sync_interval:
                    self._send_registry()
                    self._send({"kind": "ping"})
                    last_sync = now
                if now - self._last_recv > self.recv_timeout:
                    self._stop.set()  # watchdog: peer went silent
                    break
                self._stop.wait(timeout=self.poll_interval)
        finally:
            if self.peer_host is not None:
                self.store.clear_remote_agents(self.peer_host)
        return time.monotonic() - started


def serve(self_host: str | None = None) -> int:
    """Run the ``serve`` end: the pipe is this process's stdin/stdout."""
    host = self_host or socket.gethostname()
    store = StateStore()
    Bridge(sys.stdin.buffer, sys.stdout.buffer, host, store).run()
    return 0


def connect(
    host: str,
    *,
    self_host: str | None = None,
    remote_spanreed: str | None = None,
    exec_cmd: str | None = None,
    max_reconnects: int | None = None,
) -> int:
    """Run the ``connect`` end: bridge to a peer, reconnecting if the pipe dies.

    Runs in the foreground forever (until SIGINT/SIGTERM), re-establishing the
    SSH pipe with exponential backoff whenever it drops. Supervision (start on
    boot, restart on crash) is intentionally left to the user — wrap this in
    systemd/launchd/tmux if you want a service.

    ``exec_cmd`` overrides how the peer process is launched (for local testing
    without SSH). Otherwise the peer is launched over SSH; the remote
    ``spanreed`` path is given by ``remote_spanreed`` or probed via a login
    shell. ``max_reconnects`` bounds the retries (``None`` = forever).
    """
    label = self_host or socket.gethostname()
    if exec_cmd is not None:
        argv = ["sh", "-c", exec_cmd]
    else:
        remote = remote_spanreed or _probe_remote_spanreed(host)
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            # Surface a dead/half-open pipe as EOF within ~15s instead of hanging.
            "-o",
            "ServerAliveInterval=5",
            "-o",
            "ServerAliveCountMax=3",
            host,
            f"{shlex.quote(remote)} conjoin --serve",
        ]
    store = StateStore()
    return reconnect_loop(argv, label, store, max_reconnects=max_reconnects)


_BASE_BACKOFF = 1.0
_MAX_BACKOFF = 30.0
_HEALTHY_UPTIME = 30.0  # a connection lasting this long resets the backoff


def _spawn_peer(argv: list[str]) -> subprocess.Popen[bytes]:
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)


def _run_bridge_once(
    proc: subprocess.Popen[bytes],
    label: str,
    store: StateStore,
    holder: list[Bridge | None],
) -> float:
    """Bridge over one spawned peer process until the pipe dies. Returns uptime."""
    assert proc.stdin is not None and proc.stdout is not None
    bridge = Bridge(proc.stdout, proc.stdin, label, store)
    holder[0] = bridge
    try:
        return bridge.run()
    finally:
        holder[0] = None
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def reconnect_loop(
    argv: list[str],
    label: str,
    store: StateStore,
    *,
    max_reconnects: int | None = None,
    install_signals: bool = True,
    shutdown: threading.Event | None = None,
    spawn: Callable[[list[str]], subprocess.Popen[bytes]] | None = None,
    run_once: Callable[[subprocess.Popen[bytes], str, StateStore, list[Bridge | None]], float]
    | None = None,
    wait: Callable[[float], object] | None = None,
) -> int:
    """Spawn → bridge → (on death) backoff → respawn, until shutdown.

    The ``spawn``/``run_once``/``wait``/``shutdown`` seams are injectable for
    testing; the defaults use real subprocesses and a shutdown-interruptible
    sleep.
    """
    spawn = spawn or _spawn_peer
    run_once = run_once or _run_bridge_once
    shutdown = shutdown if shutdown is not None else threading.Event()
    holder: list[Bridge | None] = [None]
    waiter = wait if wait is not None else shutdown.wait

    if install_signals:
        _install_signal_handlers(shutdown, holder)

    backoff = _BASE_BACKOFF
    attempts = 0
    while not shutdown.is_set():
        proc = spawn(argv)
        uptime = run_once(proc, label, store, holder)
        if shutdown.is_set():
            break
        attempts += 1
        if max_reconnects is not None and attempts >= max_reconnects:
            break
        backoff = _BASE_BACKOFF if uptime >= _HEALTHY_UPTIME else min(backoff * 2, _MAX_BACKOFF)
        waiter(backoff + backoff * 0.5 * random.random())  # jittered
    return 0


def _install_signal_handlers(shutdown: threading.Event, holder: list[Bridge | None]) -> None:
    """Best-effort SIGINT/SIGTERM → clean shutdown. No-op off the main thread."""

    def _handler(_signum: int, _frame: object) -> None:
        shutdown.set()
        bridge = holder[0]
        if bridge is not None:
            bridge.stop()

    try:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
    except ValueError:
        pass  # not the main thread — caller drives shutdown another way


def _probe_remote_spanreed(host: str) -> str:
    """Discover the absolute path to ``spanreed`` on a peer via a login shell.

    Non-interactive SSH gets a stripped PATH, so we source the interactive
    profile (``zsh -ic``) to resolve the binary, then use the absolute path.
    """
    out = subprocess.run(
        ["ssh", host, 'zsh -ic "command -v spanreed"'],
        capture_output=True,
        text=True,
        timeout=20,
    )
    path = out.stdout.strip().splitlines()[-1].strip() if out.stdout.strip() else ""
    if not path:
        raise RuntimeError(f"could not locate remote spanreed on {host!r}: {out.stderr.strip()}")
    return path
