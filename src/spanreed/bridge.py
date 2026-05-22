"""Cross-host bus-bridge: connect two local Spanreed buses over a duplex pipe.

Design and wire-format live in ``docs/architecture.md`` and ``docs/protocol.md``.
In brief: a symmetric bridge process runs on each host, connected by one
persistent duplex pipe (``spanreed conjoin <host>`` spawns ``spanreed conjoin
--serve`` on the peer over SSH; both then run :class:`Bridge`). Each side:

- forwards messages local agents addressed to ``*@<peer>`` over the pipe,
- delivers messages arriving over the pipe into local inboxes,
- mirrors the peer's live agents into the local registry (as ``<id>@<peer>``).

Status: prototype. Point-to-point only; reconnect is not yet implemented
(EOF on the pipe tears the bridge down cleanly).
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
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
    ) -> None:
        self._read = read
        self._write = write
        self.self_host = self_host
        self.store = store
        self.poll_interval = poll_interval
        self.sync_interval = sync_interval
        self.peer_host: str | None = None
        self._stop = threading.Event()
        self._hello = threading.Event()
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

    def run(self) -> None:
        self._send({"kind": "hello", "host": self.self_host})
        reader = threading.Thread(target=self._reader, daemon=True)
        reader.start()
        if not self._hello.wait(timeout=15.0):
            self._stop.set()
            return
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
                self._stop.wait(timeout=self.poll_interval)
        finally:
            if self.peer_host is not None:
                self.store.clear_remote_agents(self.peer_host)


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
) -> int:
    """Run the ``connect`` end: spawn the peer's ``serve`` and bridge to it.

    ``exec_cmd`` overrides how the peer process is launched (used for local
    testing without SSH). Otherwise the peer is launched over SSH; the remote
    ``spanreed`` path is given by ``remote_spanreed`` or probed via a login
    shell.
    """
    label = self_host or socket.gethostname()
    if exec_cmd is not None:
        argv = ["sh", "-c", exec_cmd]
    else:
        remote = remote_spanreed or _probe_remote_spanreed(host)
        remote_invocation = f"{shlex.quote(remote)} conjoin --serve"
        argv = ["ssh", host, remote_invocation]
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert proc.stdin is not None and proc.stdout is not None
    store = StateStore()
    try:
        Bridge(proc.stdout, proc.stdin, label, store).run()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return 0


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
