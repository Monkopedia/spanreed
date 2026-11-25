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

Mirrored rows are owned by the bridge **process**, not by one connection. On
the connect end that process is ``conjoin``'s reconnect loop, which outlives a
dropped link: the peer's agents stay addressable while it reconnects, mail for
them queues in the outbound inbox, and the rows are cleared only when
``conjoin`` exits (issue #34). The serve end's process ends with its link, so
it clears them at teardown.

Status: prototype. Point-to-point only. ``conjoin`` reconnects with backoff
when the pipe dies (foreground command — supervision is the user's job);
multi-hop routing and peer-host discovery are out of scope for now.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
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
from typing import IO, Any, Literal, cast

from spanreed.protocol import Agent, Message, PeerLink
from spanreed.sleeper_remote import Target, Worker, prune_records
from spanreed.sleeper_spool import Spool
from spanreed.store import StateStore, pid_start_time

SLEEPER_OPS = "sleeper-ops"

FEATURES = [SLEEPER_OPS]
"""What this bridge process can carry, sent in ``hello`` and recorded as
``bridge_features`` (#86, "Version skew"). Capabilities, never a version."""


def _status(text: str) -> None:
    """One timestamped line on stderr for the human watching `conjoin`.

    `conjoin` runs in the foreground and the owner watches its terminal, so
    every connect, disconnect and retry is printed rather than left for
    `spanreed list` to reveal later. stderr, because on the serve end stdout IS
    the pipe -- though only the connect end calls this, since ssh forwards the
    serve end's stderr to the same terminal and every event would print twice.
    """
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] spanreed conjoin: {text}", file=sys.stderr, flush=True)


def _duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


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
        hello_timeout: float = 15.0,
        mirror_outlives_link: bool = False,
        sleeper_worker: Worker | None = None,
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
        # How long run() waits for the peer's hello. A parameter so the
        # no-handshake path can be tested without a 15-second test.
        self.hello_timeout = hello_timeout
        # True under `conjoin`'s reconnect loop, which owns the mirrored rows
        # for its whole life and clears them when it exits (#34). False for a
        # bridge whose process ends with its link -- the serve end -- which
        # clears them itself at teardown.
        self.mirror_outlives_link = mirror_outlives_link
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
        # Teardown ends this link's say over the mirror, but the reader thread
        # is still running then and can apply a registry frame the peer had
        # already sent. Without a guard that frame re-mirrors rows AFTER they
        # were cleared (#81): on the serve end, after teardown's own clear; under
        # `conjoin`, after its exit-time clear, or over the snapshot of the next
        # connection. Those rows are owned by a live pid, so they would read as
        # LIVE with nothing behind them. One lock and a flag make "tear down"
        # and "apply" mutually exclusive and final.
        self._mirror_lock = threading.Lock()
        self._torn_down = False
        # Why the link ended, for the disconnect line. First writer wins: the
        # watchdog and an external stop() name the cause, and the reader's EOF
        # that usually follows them must not overwrite it.
        self._end_reason: str | None = None
        # Set by stop(final=True): this teardown is the process exiting.
        self._final = False
        # Remote sleeper management (#86). The worker belongs to the bridge
        # PROCESS: `conjoin` passes one that outlives each link and closes it
        # when it exits. A bridge given none (the serve end, whose process ends
        # with its link) makes its own and closes it when run() returns.
        self._worker = sleeper_worker if sleeper_worker is not None else Worker()
        self._owns_worker = sleeper_worker is None
        # Set at the hello, once the peer has named itself.
        self._target: Target | None = None
        self._spool: Spool | None = None
        self._peer_features: list[str] | None = None
        # Answers for the main thread to send, each with the op it answers (a
        # create's or retire's goes out behind a fresh registry frame). Filled
        # by the reader (answers from a record) and by the worker.
        self._results: queue.Queue[tuple[object, dict[str, Any]]] = queue.Queue()
        # Requests another live process holds, by id: the main thread sends
        # each one's result once its record is done.
        self._watch_requests: queue.Queue[dict[str, Any]] = queue.Queue()
        self._watching: dict[str, dict[str, Any]] = {}
        # Requests held because the peer did not advertise sleeper-ops; each is
        # noted on the peer record once.
        self._held_noted: set[str] = set()

    # --- framing (only the main thread writes, so no lock needed) ---

    def _send(self, frame: dict[str, Any]) -> bool:
        """Write a frame to the pipe. Returns False (and stops) on a dead pipe."""
        try:
            self._write.write((json.dumps(frame) + "\n").encode())
            self._write.flush()
            return True
        except (BrokenPipeError, ValueError):
            self._stop.set()
            return False

    def stop(self, *, final: bool = False) -> None:
        """Signal the bridge to shut down (cleared on the next loop turn).

        ``final`` says the process is exiting, not reconnecting: teardown then
        writes ``stopping_at`` in the same record write as ``detached_at``, so
        the link never reads as reconnecting on its way out (#34).
        """
        if final:
            self._final = True
        self._end("shutting down")
        self._stop.set()

    def _end(self, reason: str) -> None:
        if self._end_reason is None:
            self._end_reason = reason

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
        self._end("the pipe closed")
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
                self._end(f"refused the peer's invalid host label {host!r}")
                self._stop.set()
                self._hello.set()  # unblock run()'s wait so it exits promptly
                return True
            peer_features, bad_features = _parse_features(frame.get("features"))
            with self._mirror_lock:
                if self._torn_down:
                    # run() already gave up on the handshake and returned. A
                    # peer accepted now would be mirrored by a bridge nobody is
                    # running, and read as live while nothing is connected.
                    return True
                self.peer_host = host
                self._peer_features = peer_features
                self._target = Target(
                    self.store,
                    self_host=self.self_host,
                    peer=host,
                    role=self.role,
                    bridge_pid=self._my_pid,
                    bridge_pid_start=self._my_pid_start,
                )
                self._spool = Spool(self.store, host)
                self._open_link(host)
            if bad_features:
                self._note(
                    f"the peer's hello carried a 'features' field that is not a list of "
                    f"strings ({str(frame.get('features'))[:120]!r}). It was recorded as "
                    f"null, so no remote sleeper request is sent to this peer."
                )
            self._hello.set()
        elif kind == "msg":
            self.store.append_message(Message.model_validate(frame["message"]))
        elif kind == "registry":
            self._apply_registry(frame)
        elif kind == "registry-request":
            # The peer wants a snapshot now rather than on our next tick.
            # Answered by the main thread; see _registry_requested.
            self._registry_requested.set()
        elif kind == "sleeper-op":
            self._receive_sleeper_op(frame)
        elif kind == "sleeper-op-result":
            if self._spool is None:
                self._note(
                    "a 'sleeper-op-result' frame arrived BEFORE the peer's hello and was dropped."
                )
            else:
                note = self._spool.accept_result(frame)
                if note is not None:
                    self._note(note)
        # "ping" needs no action — it just keeps the pipe warm.
        return False

    def _receive_sleeper_op(self, frame: dict[str, Any]) -> None:
        """A peer asks this host to run a sleeper operation. Never run on this thread (#86).

        :meth:`Target.receive` decides what the frame gets (the validation, the
        switch, the expiry and the idempotency record), and the reader hands it
        on: an answer to the main thread, a new request to the worker, a request
        another live process holds to the main thread's watch.
        """
        if self._target is None:
            self._note(
                "a 'sleeper-op' frame arrived BEFORE the peer's hello and was dropped unanswered."
            )
            return
        receipt = self._target.receive(frame)
        op = frame.get("op")
        if receipt.action == "reply":
            assert receipt.result is not None
            self._results.put((op, receipt.result))
        elif receipt.action == "run":
            assert receipt.request_id is not None
            self._worker.submit(self._target, receipt.request_id, self._delivery(op, frame))
        elif receipt.action == "wait":
            self._watch_requests.put(frame)
        elif receipt.note is not None:
            self._note(receipt.note)

    def _delivery(
        self, op: object, frame: dict[str, Any]
    ) -> Callable[[dict[str, Any] | None], None]:
        """The worker's way back to this link's main thread.

        If the link has ended by then, nothing sends it: the result is in the
        record, and the resend after a reconnect is answered from there.
        """

        def deliver(result: dict[str, Any] | None) -> None:
            if result is not None:
                self._results.put((op, result))
            else:
                self._watch_requests.put(frame)  # another process holds it

        return deliver

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
        with self._mirror_lock:
            if self._torn_down:
                return  # arrived after teardown cleared the mirror; see __init__
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
                bridge_features=list(FEATURES),
                peer_features=self._peer_features,
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
        # Asleep sleepers are advertised too, with their `sleeper` state, so a
        # peer can mail them: the mail is what wakes them.
        sleeper_ids = self.store.sleeper_ids()
        live = [a for a in rows if self.store.is_present(a, sleeper_ids)]
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

    # --- remote sleeper management (main thread) ---

    def _send_sleeper_results(self) -> None:
        """Send every answer that is ready, a registry frame ahead of a create's or retire's.

        Frames arrive in order on one pipe, so the requester has mirrored the new
        sleeper (or dropped the retired one) by the time its CLI returns.
        """
        while True:
            try:
                op, result = self._results.get_nowait()
            except queue.Empty:
                return
            if op in ("create", "retire") and not self._send_registry():
                return
            if not self._send(result):
                return

    def _check_watched(self) -> None:
        """Queue the answer to each request another live process held, once it is done."""
        assert self._target is not None
        while True:
            try:
                frame = self._watch_requests.get_nowait()
            except queue.Empty:
                break
            self._watching[str(frame["request_id"])] = frame
        for request_id, frame in list(self._watching.items()):
            result = self._target.result_if_done(request_id)
            if result is not None:
                del self._watching[request_id]
                self._results.put((frame.get("op"), result))

    def _hold(self, unsent: list[str], sent: list[str]) -> None:
        """Say on the peer record why requests are not being sent. Once per request.

        ``unsent`` are ``.json`` requests, never sent: they expire unsent.
        ``sent`` are ``.sent`` requests (on attach only), sent over an earlier
        link and now not resent: whether they ran is not known here.
        """
        new_unsent = [rid for rid in unsent if rid not in self._held_noted]
        new_sent = [rid for rid in sent if rid not in self._held_noted]
        if not new_unsent and not new_sent:
            return
        self._held_noted.update(new_unsent + new_sent)
        said = (
            "sent no features in its hello (its spanreed predates remote sleeper management)"
            if self._peer_features is None
            else f"listed features {self._peer_features} in its hello, without {SLEEPER_OPS!r}"
        )
        where = f"sleeper-ops/out/{self.peer_host}/"
        parts = [f"{self.peer_host} {said}, so it would ignore sleeper requests."]
        if new_unsent:
            parts.append(
                f"Request(s) {', '.join(new_unsent)} in {where} were NOT sent, and expire unsent."
            )
        if new_sent:
            parts.append(
                f"Request(s) {', '.join(new_sent)} in {where} were sent over an earlier link and "
                f"are NOT resent; their outcome is unknown here (check `spanreed sleeper list "
                f"--host {self.peer_host}` and its audit log)."
            )
        parts.append(f"Upgrade spanreed on {self.peer_host} and restart its end of `conjoin`.")
        self._note(" ".join(parts))

    def _drain_spool(self, *, attach: bool) -> None:
        """Send this host's requests for the peer; on ``attach``, resend those in flight.

        A request is sent only before its ``expires_at``, and only to a peer
        that advertised ``sleeper-ops``: an older peer drops the frame without a
        word, so a request for one is held: one never sent expires unsent, and
        one already sent over an earlier link is not resent. Expiry is
        enforced here whatever its CLI does, so a killed CLI's request is never
        sent late ("Every request expires").
        """
        spool = self._spool
        assert spool is not None
        spool.expire()
        if self._peer_features is None or SLEEPER_OPS not in self._peer_features:
            self._hold(spool.ids(".json"), spool.ids(".sent") if attach else [])
            return
        if attach:
            for frame in spool.resend():
                if not self._send(frame):
                    return
        scan = spool.take_new()
        for note in scan.notes:
            self._note(note)
        for frame in scan.frames:
            if not self._send(frame):
                return

    def _sleeper_turn(self, *, attach: bool = False) -> None:
        """One main-loop turn of remote sleeper management, in both directions."""
        self._check_watched()
        self._send_sleeper_results()
        if not self._stop.is_set():
            self._drain_spool(attach=attach)

    def run(self) -> float:
        """Run until the pipe dies or :meth:`stop` is called. Returns uptime (s)."""
        try:
            return self._run()
        finally:
            if self._owns_worker:
                # This process ends with its link: an operation in flight
                # finishes and stores its result first ("When things break").
                self._worker.close()

    def _run(self) -> float:
        started = time.monotonic()
        self._send({"kind": "hello", "host": self.self_host, "features": list(FEATURES)})
        reader = threading.Thread(target=self._reader, daemon=True)
        reader.start()
        if not self._hello.wait(timeout=self.hello_timeout):
            # Final, the way teardown is: the reader checks this flag under the
            # same lock before accepting a hello, so a hello that lands after
            # this point opens no peer record and mirrors nothing. One that won
            # the lock first was accepted in time, and the link proceeds.
            with self._mirror_lock:
                self._torn_down = self.peer_host is None
        if self._torn_down:
            self._stop.set()
            if self.role == "connect":
                _status(
                    f"could not connect: the peer sent no handshake within {self.hello_timeout:.0f}s "
                    "(is spanreed installed on the far side?)"
                )
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
            if self.role == "connect":
                _status(f"connected to {self.peer_host}")
            self._send_registry()
            # Push and pull in the same breath. The push alone is what shipped
            # before #55, and it is only half a handshake: it tells the peer
            # about us and leaves us with no way to insist on the reverse.
            self._request_registry()
            self._sleeper_turn(attach=True)
            last_sync = time.monotonic()
            while not self._stop.is_set():
                self._forward_outbound()
                self._sleeper_turn()
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
                    if self._spool is not None:
                        with contextlib.suppress(OSError):
                            self._spool.clean()
                    self._update_link(last_frame_at=self._recv_wallclock())
                    last_sync = now
                if now - self._last_recv > self.recv_timeout:
                    self._end(f"no frame from the peer for {self.recv_timeout:.0f}s")
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
            if self.role == "connect":
                who = self.peer_host or "the peer"
                why = self._end_reason or "the bridge stopped"
                _status(
                    f"disconnected from {who} after {_duration(time.monotonic() - started)}: {why}"
                )
            if self.peer_host is not None:
                with self._mirror_lock:
                    self._torn_down = True
                    if not self.mirror_outlives_link:
                        self.store.clear_remote_agents(self.peer_host)
                # The record is updated, never deleted: "a bridge to this host
                # was up and is now gone" is a diagnosis, and removing the file
                # would make it indistinguishable from "no bridge was ever
                # configured" — two states with different remedies.
                now = datetime.now(UTC)
                if self._final:
                    self._update_link(detached_at=now, stopping_at=now)
                else:
                    self._update_link(detached_at=now)
        return time.monotonic() - started

    def _recv_wallclock(self) -> datetime:
        """Wall-clock time of the last frame received.

        ``_last_recv`` is a monotonic reading (correct for the watchdog, which
        must not be fooled by a clock step) and meaningless on disk, so it is
        converted here against the same monotonic reference. Both clocks are
        read at once, so the conversion is exact to within one loop turn.
        """
        return datetime.now(UTC) - timedelta(seconds=time.monotonic() - self._last_recv)


DEFAULT_TIMEOUT = 15.0
"""Seconds of silence after which a link is declared dead, on both ends."""

DEFAULT_HANDSHAKE_TIMEOUT = 15.0
"""Seconds to wait for the peer's hello after the pipe opens."""

_PING_INTERVAL = 3.0  # Bridge's default sync_interval: the peer pings this often
MIN_TIMEOUT = 2 * _PING_INTERVAL
"""Below this, one late ping is enough to tear a healthy link down."""

MAX_TIMEOUT = 86400.0
"""One day. Above roughly this, `Event.wait` raises OverflowError, and a link
silent for longer than a day is not a slow link."""


def _parse_features(raw: object) -> tuple[list[str] | None, bool]:
    """A peer's ``hello`` ``features``: the list, and whether it was malformed.

    Absent is ``None``, "too old to say", never an empty list. A value that is
    not a list of strings is one this version can't model; it reads as ``None``
    too, since it can't be shown to carry anything, and is reported.
    """
    if raw is None:
        return None, False
    if isinstance(raw, list) and all(isinstance(f, str) for f in cast("list[object]", raw)):
        return [str(f) for f in cast("list[object]", raw)], False
    return None, True


def _prune_at_start(store: StateStore) -> None:
    """Prune idempotency records past retention, as a bridge process starts (R5).

    Before any :class:`Target` exists in this process: ``prune_records`` takes
    each peer's records lock, and this process must not already hold one.
    A prune that fails is not a reason to refuse the link.
    """
    with contextlib.suppress(OSError):
        prune_records(store)


def serve(self_host: str | None = None, *, timeout: float | None = None) -> int:
    """Run the ``serve`` end: the pipe is this process's stdin/stdout.

    ``timeout`` is passed by the connect end when the user set ``--timeout``,
    so both watchdogs agree: a far end still on 15s would tear the link down
    long before a patient near end noticed anything.
    """
    host = self_host or socket.gethostname()
    store = StateStore()
    _prune_at_start(store)
    Bridge(
        sys.stdin.buffer,
        sys.stdout.buffer,
        host,
        store,
        role="serve",
        recv_timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
    ).run()
    return 0


def _ssh_keepalive(timeout: float) -> list[str]:
    """ssh keepalive options that give up after roughly ``timeout`` seconds.

    ssh has its own dead-link detector, and it runs independently of the
    bridge's watchdog: raising only the watchdog leaves ssh dropping the pipe
    at its own ~15s, so the knob would appear to do nothing.
    """
    count = 3
    interval = max(1, round(timeout / count))
    return [
        "-o",
        f"ServerAliveInterval={interval}",
        "-o",
        f"ServerAliveCountMax={count}",
    ]


def connect(
    host: str,
    *,
    self_host: str | None = None,
    remote_spanreed: str | None = None,
    exec_cmd: str | None = None,
    max_reconnects: int | None = None,
    timeout: float | None = None,
    handshake_timeout: float | None = None,
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

    ``timeout`` (silence before a link is declared dead) sets all three timers
    that can end a link -- this end's watchdog, ssh's keepalive, and the far
    end's watchdog -- so they agree. It is forwarded to the far end only when
    set, because an older peer's ``conjoin --serve`` rejects the flag.
    ``handshake_timeout`` bounds the wait for the peer's hello and the remote
    path probe, both of which a slow ssh login stretches.
    """
    label = self_host or socket.gethostname()
    silence = timeout if timeout is not None else DEFAULT_TIMEOUT
    hello = handshake_timeout if handshake_timeout is not None else DEFAULT_HANDSHAKE_TIMEOUT
    if exec_cmd is not None:
        argv = ["sh", "-c", exec_cmd]
    else:
        remote = remote_spanreed or _probe_remote_spanreed(host, timeout=max(20.0, hello))
        serve_cmd = f"{shlex.quote(remote)} conjoin --serve"
        if timeout is not None:
            serve_cmd += f" --timeout {timeout:g}"
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            # Surface a dead/half-open pipe as EOF instead of hanging.
            *_ssh_keepalive(silence),
            host,
            serve_cmd,
        ]
    store = StateStore()
    _prune_at_start(store)
    # One worker for the whole `conjoin` process, outliving each link: a
    # request queued here still runs after a drop, and the resend after the
    # reconnect is answered from its record (#86).
    worker = Worker()

    def run_once(
        proc: subprocess.Popen[bytes], label: str, store: StateStore, holder: list[Bridge | None]
    ) -> float:
        return _run_bridge_once(
            proc,
            label,
            store,
            holder,
            recv_timeout=silence,
            hello_timeout=hello,
            sleeper_worker=worker,
        )

    try:
        return reconnect_loop(
            argv, label, store, max_reconnects=max_reconnects, peer=host, run_once=run_once
        )
    finally:
        worker.close()


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
    *,
    recv_timeout: float = DEFAULT_TIMEOUT,
    hello_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
    sleeper_worker: Worker | None = None,
) -> float:
    """Bridge over one spawned peer process until the pipe dies. Returns uptime."""
    assert proc.stdin is not None and proc.stdout is not None
    bridge = Bridge(
        proc.stdout,
        proc.stdin,
        label,
        store,
        role="connect",
        recv_timeout=recv_timeout,
        hello_timeout=hello_timeout,
        mirror_outlives_link=True,  # reconnect_loop clears them when conjoin exits
        sleeper_worker=sleeper_worker,
    )
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
    peer: str | None = None,
) -> int:
    """Spawn → bridge → (on death) backoff → respawn, until shutdown.

    ``peer`` names the far side in the status lines; it is display-only.

    This loop is what owns the peer's mirrored agents: they are written under
    this process's pid, survive every dropped link so mail for the peer keeps
    queuing while it reconnects, and are cleared here when the loop ends
    (issue #34). A ``conjoin`` killed before it gets here leaves them behind
    with a dead owner pid, which the resolver refuses.

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
    target = peer or "peer"
    try:
        while not shutdown.is_set():
            _status(
                f"connecting to {target}..." if attempts == 0 else f"reconnecting to {target}..."
            )
            proc = spawn(argv)
            uptime = run_once(proc, label, store, holder)
            if shutdown.is_set():
                break
            attempts += 1
            if max_reconnects is not None and attempts >= max_reconnects:
                _status(f"giving up after {attempts} attempt(s) (--max-reconnects)")
                break
            backoff = _BASE_BACKOFF if uptime >= _HEALTHY_UPTIME else min(backoff * 2, _MAX_BACKOFF)
            delay = backoff + backoff * 0.5 * random.random()  # jittered
            _status(f"retrying in {delay:.0f}s")
            waiter(delay)
    finally:
        pid = os.getpid()
        start = pid_start_time(pid)
        # Order matters (#34): the record must read as stopping before the rows
        # go, or for the tens of milliseconds until this process exits a send
        # finds no row, a live pid and a detached link, and is told the
        # reconnect will forward it.
        store.mark_peer_links_stopping(pid, start)
        store.clear_agents_owned_by(pid, start)
    _status("stopped")
    return 0


def _install_signal_handlers(shutdown: threading.Event, holder: list[Bridge | None]) -> None:
    """Best-effort SIGINT/SIGTERM → clean shutdown. No-op off the main thread."""

    def _handler(_signum: int, _frame: object) -> None:
        shutdown.set()
        bridge = holder[0]
        if bridge is not None:
            bridge.stop(final=True)

    try:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
    except ValueError:
        pass  # not the main thread — caller drives shutdown another way


def _probe_remote_spanreed(host: str, *, timeout: float = 20.0) -> str:
    """Discover the absolute path to ``spanreed`` on a peer via a login shell.

    Non-interactive SSH gets a stripped PATH, so we source the interactive
    profile (``zsh -ic``) to resolve the binary, then use the absolute path.
    """
    out = subprocess.run(
        ["ssh", host, 'zsh -ic "command -v spanreed"'],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    path = out.stdout.strip().splitlines()[-1].strip() if out.stdout.strip() else ""
    if not path:
        raise RuntimeError(f"could not locate remote spanreed on {host!r}: {out.stderr.strip()}")
    return path
