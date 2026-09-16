"""Cross-host bus-bridge: connect two local Spanreed buses over a duplex pipe.

Design and wire-format live in ``docs/architecture.md`` and ``docs/protocol.md``.
In brief: a symmetric bridge process runs on each host, connected by one
persistent duplex pipe (``spanreed conjoin <host>`` spawns ``spanreed conjoin
--serve`` on the peer over SSH; both then run :class:`Bridge`). Each side:

- forwards messages local agents addressed to ``*@<peer>`` over the pipe,
- delivers messages arriving over the pipe into local inboxes,
- mirrors the peer's live agents into the local registry (as ``<id>@<peer>``),
- records what it is seeing in ``peers/<peer>.json`` (see :class:`PeerLink`).

Registry sync is **both a push and a pull**. Each side advertises its own agents
on a timer, and also *asks* the peer to advertise (``registry-request``) until a
snapshot actually arrives. The push alone is what issue #55 was reported
against: it makes one side's silence invisible to the other, so a host that
never learns its peer's agents cannot tell "the peer has none" from "the peer
never spoke" from "I dropped what it said". The pull turns that into a question
this side asks and can count, and the peer record makes every one of those
outcomes readable without attaching a debugger to either host.

Status: prototype. Point-to-point only. ``conjoin`` reconnects with backoff
when the pipe dies (foreground command — supervision is the user's job);
multi-hop routing and peer-host discovery are out of scope for now.
"""

from __future__ import annotations

import contextlib
import json
import os
import random
import re
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import IO, Literal, cast

from spanreed.protocol import Agent, Message, PeerLink
from spanreed.store import StateStore, is_stale, pid_start_time

_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
"""A plausible host label: alphanumeric ends, dots/hyphens/underscores inside.

This validates the label a peer *advertises for itself* (usually its
``gethostname()``, or whatever ``--label`` overrides it with) — not the SSH
target we dialled. Deliberately permissive about what such a name may contain,
since it can be an mDNS ``.local`` name, an IPv4 literal or a container name,
and strict about what it may NOT: no glob metacharacters, no ``/``, no
whitespace, not empty.
"""


def _is_valid_host(host: str) -> bool:
    """True if ``host`` is safe to use as a routing suffix and a glob literal.

    The peer chooses this string and we interpolate it into ``Path.glob`` and
    into ``@``-suffix comparisons. A ``*`` there is a wildcard, not a name.
    """
    return len(host) <= 253 and _HOST_RE.fullmatch(host) is not None


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
        role: Literal["connect", "serve"] = "connect",
        poll_interval: float = 0.5,
        sync_interval: float = 3.0,
        recv_timeout: float | None = None,
    ) -> None:
        self._read = read
        self._write = write
        self.self_host = self_host
        self.store = store
        self.role: Literal["connect", "serve"] = role
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
        # Peer record. Mutated from both threads (the reader counts what
        # arrives, the main thread counts what it asks for), so it is guarded;
        # the file write happens under the same lock to keep the record on disk
        # internally consistent.
        self._link: PeerLink | None = None
        self._link_lock = threading.Lock()
        # Set once a registry frame from the peer has actually been applied.
        # While it is clear, the main loop keeps asking — that is the "pull"
        # half of sync, and the thing whose absence issue #55 could not see.
        self._peer_registry_seen = threading.Event()
        # The peer asked US to advertise. Handled by the main thread on its next
        # turn rather than by the reader, because only the main thread writes to
        # the pipe and that invariant is what makes _send lock-free.
        self._registry_requested = threading.Event()
        # A diagnosis observed before there was a peer record to write it to.
        self._deferred_note: str | None = None

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

    def _note(self, text: str) -> None:
        """Attach a human-readable diagnosis to the peer record, if there is one.

        Before the handshake there is no record yet — the host it would be filed
        under is exactly what has not arrived — so the note is held and attached
        when the record is created. Held, not dropped: junk on the pipe before
        the hello (an SSH banner, a shell profile that prints) is a leading cause
        of a bridge that never completes its handshake, and it must not be the
        one event that leaves no trace.
        """
        with self._link_lock:
            if self._link is None:
                self._deferred_note = text
                return
            self._link.note = text
            self._persist_link()

    def _update_link(self, **fields: object) -> None:
        """Apply fields to the peer record and persist it. No-op before the hello."""
        with self._link_lock:
            if self._link is None:
                return
            for key, value in fields.items():
                setattr(self._link, key, value)
            self._persist_link()

    def _persist_link(self) -> None:
        """Write the peer record, swallowing IO errors. Caller holds ``_link_lock``.

        The record exists to make faults visible; it must never be able to
        *cause* one. A full disk or a read-only state root would otherwise take
        down the reader thread through the very call meant to report trouble —
        same reasoning as the MCP server's ``_log``.
        """
        assert self._link is not None
        with contextlib.suppress(OSError):
            self.store.write_peer_link(self._link)

    def _reader(self) -> None:
        for raw in self._read:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            try:
                frame = json.loads(line)
                kind = frame.get("kind")
            except (json.JSONDecodeError, AttributeError):
                self._note(
                    "the peer sent a line that is not a JSON bus frame, and it was "
                    f"ignored: {line[:200]!r}. If this repeats, something other than "
                    "spanreed is writing to the pipe on the far side — a login banner "
                    "or a shell profile that prints on non-interactive ssh."
                )
                continue
            self._last_recv = time.monotonic()  # any frame proves the pipe is alive
            try:
                if self._handle_frame(kind, frame):
                    return
            except Exception as exc:  # a bad frame must not kill the reader
                # Before #55 this except did not exist, and any frame the peer
                # sent that this version could not model (a version skew in the
                # Agent record, say) killed the reader thread outright. The
                # bridge then looked alive — the main thread kept forwarding
                # mail — while silently ingesting nothing, which is one of the
                # ways the reported symptom is produced. Record and carry on.
                self._note(
                    f"a {kind!r} frame from the peer could not be processed and was "
                    f"DROPPED: {type(exc).__name__}: {exc}. The bridge is still up and "
                    "still carrying messages; if this frame was 'registry', the peer's "
                    "agents are not addressable from this host until it can be applied. "
                    "A version skew between the two hosts' spanreed is the usual cause "
                    "— upgrade both ends to the same release."
                )
        self._stop.set()  # EOF: peer/pipe gone.

    def _handle_frame(self, kind: object, frame: dict[str, object]) -> bool:
        """Act on one frame. Returns True when the reader must stop (refused peer)."""
        if kind == "hello":
            host = str(frame["host"])
            if not _is_valid_host(host):
                # Refuse the connection rather than routing on it. This value
                # reaches Path.glob(); "*" there matches every host-qualified
                # inbox, including mail queued for an unrelated peer.
                print(
                    f"spanreed: the peer advertised an invalid host label {host!r}; "
                    "refusing the bridge. A label must be alphanumeric at both ends "
                    "with only '.', '-' or '_' inside, and at most 253 characters. "
                    "The label belongs to the OTHER end, so it has to be fixed "
                    "there: pass --label to whichever `spanreed conjoin` runs on "
                    "that machine, or change its hostname. Caveat: `conjoin <host>` "
                    "launches the remote end with no way to pass --label, so a peer "
                    "on the serve side may have no remedy but its hostname "
                    "(spanreed#27).",
                    file=sys.stderr,
                )
                self._stop.set()
                self._hello.set()  # unblock run()'s wait so it exits promptly
                return True
            self.peer_host = host
            self._open_link(host)
            self._hello.set()
        elif kind == "msg":
            self.store.append_message(Message.model_validate(frame["message"]))
        elif kind == "registry":
            self._apply_registry(frame)
        elif kind == "registry-request":
            # The peer wants a snapshot now rather than on our next tick.
            # Answered by the main thread; see _registry_requested.
            self._registry_requested.set()
        # "ping" needs no action — it just keeps the pipe warm.
        return False

    def _apply_registry(self, frame: dict[str, object]) -> None:
        """Mirror a peer's registry snapshot locally and record that it arrived."""
        if self.peer_host is None:
            # Pre-#55 this was a bare `if peer_host is not None:` with no else,
            # so a registry frame that raced ahead of the hello vanished without
            # a trace and the peer's agents stayed unaddressable until its next
            # push. It is now counted, and the pull in run() re-asks, so the
            # race self-heals instead of costing a whole sync interval of
            # silence with nothing to show for it.
            self._note(
                "a 'registry' frame arrived BEFORE the peer's hello and was dropped "
                "(there was no host to file its agents under yet). A registry-request "
                "will be sent once the handshake completes, so this is self-healing; "
                "it is recorded because an unexplained gap in sync is exactly what "
                "issue #55 could not diagnose."
            )
            return
        raw_agents = cast("list[object]", frame["agents"])
        agents = [Agent.model_validate(a) for a in raw_agents]
        self.store.sync_remote_agents(self.peer_host, agents, self._my_pid, self._my_pid_start)
        self._peer_registry_seen.set()
        now = datetime.now(UTC)
        with self._link_lock:
            if self._link is None:
                return
            self._link.last_frame_at = now
            self._link.last_registry_at = now
            self._link.last_registry_agents = len(agents)
            # Absent from a peer running a spanreed older than #55; None then,
            # which reads as "this peer cannot tell us" rather than as zero.
            rows = frame.get("local_rows")
            stale = frame.get("stale_local_rows")
            self._link.peer_registry_rows = rows if isinstance(rows, int) else None
            self._link.peer_stale_rows = stale if isinstance(stale, int) else None
            self._link.registry_syncs += 1
            self._persist_link()

    def _open_link(self, host: str) -> None:
        """Create and persist the peer record, now that the peer has named itself."""
        now = datetime.now(UTC)
        with self._link_lock:
            self._link = PeerLink(
                host=host,
                role=self.role,
                bridge_pid=self._my_pid,
                bridge_pid_start=self._my_pid_start,
                attached_at=now,
                last_frame_at=now,
                note=self._deferred_note,
            )
            self._deferred_note = None
            self._persist_link()

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

    def _send_registry(self) -> bool:
        """Advertise this host's live local agents to the peer.

        The frame carries the counts behind the list as well as the list: how
        many bare local rows this host's registry holds, and how many of them
        this host judged stale and therefore withheld. An empty ``agents`` list
        is otherwise indistinguishable on the receiving end from "the peer is
        not answering", and the difference between those two is the difference
        between a broken bridge and a host whose own agents have died — which is
        the fork issue #55 had no way to take.
        """
        rows = [a for a in self.store.list_agents(include_stale=True) if "@" not in a.agent_id]
        live = [a for a in rows if not is_stale(a)]
        return self._send(
            {
                "kind": "registry",
                "agents": [a.model_dump(mode="json") for a in live],
                "local_rows": len(rows),
                "stale_local_rows": len(rows) - len(live),
            }
        )

    def _request_registry(self) -> bool:
        """Ask the peer to advertise its agents now.

        The pull half of sync. A peer that is healthy answers immediately; a
        peer that never answers leaves ``registry_requests_sent`` climbing with
        ``last_registry_at`` still ``never``, which is a readable, countable
        statement of the exact fault issue #55 was reported for. Old peers
        ignore the frame (their reader has no branch for it) and keep pushing on
        their own timer, so this is safe against version skew in both
        directions.
        """
        sent = self._send({"kind": "registry-request"})
        if sent:
            with self._link_lock:
                if self._link is not None:
                    self._link.registry_requests_sent += 1
                    self._persist_link()
        return sent

    def run(self) -> float:
        """Run until the pipe dies or :meth:`stop` is called. Returns uptime (s)."""
        started = time.monotonic()
        self._send({"kind": "hello", "host": self.self_host})
        reader = threading.Thread(target=self._reader, daemon=True)
        reader.start()
        if not self._hello.wait(timeout=15.0):
            self._stop.set()
            return time.monotonic() - started
        try:
            if self.peer_host is None:
                # The reader refused the peer and set _hello only to release the
                # wait above. Return before advertising anything: the registry
                # frame carries agent ids, display names, absolute working
                # directories, pids and focus text, and a peer we just hung up
                # on must not get it.
                #
                # Two independent properties here, and it is worth not
                # confusing them:
                #
                # Inside the `try` so the `finally` runs. `_stop` is also set by
                # a plain EOF, which can land here after a VALID hello, and that
                # case has mirrored entries to tear down; an early return above
                # the `try` skipped `clear_remote_agents` for it.
                #
                # Keyed on `peer_host` because that is what the branch is
                # actually about — "we never accepted this peer" — where `_stop`
                # only means "stop". Either change alone closes the teardown
                # hole; both are kept because the predicate is the honest one
                # and the placement is the robust one. They do diverge: an
                # external stop() between the hello and this guard sets `_stop`
                # with a valid peer_host, and only the `peer_host` form still
                # advertises the registry. Neither is wrong; they are different.
                return time.monotonic() - started
            self._last_recv = time.monotonic()
            self._send_registry()
            # Push and pull in the same breath. The push alone is what shipped
            # before #55, and it is only half a handshake: it tells the peer
            # about us and leaves us with no way to insist on the reverse.
            self._request_registry()
            last_sync = time.monotonic()
            while not self._stop.is_set():
                self._forward_outbound()
                if self._registry_requested.is_set():
                    # Answer the peer's pull promptly rather than at our next
                    # tick. Sent from this thread, never the reader's, so the
                    # single-writer property _send relies on still holds.
                    self._registry_requested.clear()
                    self._send_registry()
                now = time.monotonic()
                if now - last_sync >= self.sync_interval:
                    self._send_registry()
                    if not self._peer_registry_seen.is_set():
                        # Keep asking until something actually arrives. Bounded
                        # by the watchdog below, not by a retry count: while the
                        # pipe is alive, a peer that has not advertised is a
                        # fault worth re-asking about on every tick, and each
                        # ask is one line on a pipe that is already sending a
                        # keepalive.
                        self._request_registry()
                    self._send({"kind": "ping"})
                    self._update_link(last_frame_at=self._recv_wallclock())
                    last_sync = now
                if now - self._last_recv > self.recv_timeout:
                    self._stop.set()  # watchdog: peer went silent
                    self._note(
                        f"the peer went silent: no frame of any kind arrived within "
                        f"{self.recv_timeout:.0f}s, so the pipe was treated as dead and "
                        "this link was torn down. `conjoin` re-establishes it with "
                        "backoff; a link that keeps reappearing here is a flapping SSH "
                        "connection, not a spanreed fault."
                    )
                    break
                self._stop.wait(timeout=self.poll_interval)
        finally:
            if self.peer_host is not None:
                self.store.clear_remote_agents(self.peer_host)
                # The record is updated, never deleted: "a bridge to this host
                # was up and is now gone" is a diagnosis, and removing the file
                # would make it indistinguishable from "no bridge was ever
                # configured" — two states with different remedies.
                self._update_link(detached_at=datetime.now(UTC))
        return time.monotonic() - started

    def _recv_wallclock(self) -> datetime:
        """Wall-clock time of the last frame received.

        ``_last_recv`` is a monotonic reading (correct for the watchdog, which
        must not be fooled by a clock step) and meaningless on disk, so it is
        converted here against the same monotonic reference. Both clocks are
        read at once, so the conversion is exact to within one loop turn.
        """
        return datetime.now(UTC) - timedelta(seconds=time.monotonic() - self._last_recv)


def serve(self_host: str | None = None) -> int:
    """Run the ``serve`` end: the pipe is this process's stdin/stdout."""
    host = self_host or socket.gethostname()
    store = StateStore()
    Bridge(sys.stdin.buffer, sys.stdout.buffer, host, store, role="serve").run()
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
    bridge = Bridge(proc.stdout, proc.stdin, label, store, role="connect")
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
