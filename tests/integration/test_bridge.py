"""Integration test for the cross-host bus-bridge.

Wires two ``_Bridge`` instances together over in-process pipes (standing in
for the SSH duplex pipe), each backed by its own StateStore, and checks that
agents mirror across and messages flow in both directions.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from types import SimpleNamespace
from typing import IO, cast
from unittest.mock import MagicMock

import pytest

from spanreed import bridge
from spanreed.bridge import Bridge, reconnect_loop
from spanreed.store import StateStore

# Aliased once so the private stays private — same pattern as test_cli.py's
# `_parse_since`: one alias costs a single ignore instead of one per call site.
_is_valid_host = bridge._is_valid_host  # pyright: ignore[reportPrivateUsage]


def _wait(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


def _register_live(store: StateStore, agent_id: str, name: str) -> None:
    store.register_agent(name=name, working_dir="/tmp", pid=os.getpid(), agent_id=agent_id)


def _dead_pid() -> int:
    """Spawn-and-reap a subprocess to get a PID guaranteed not in use."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _synced(store: StateStore, host: str) -> bool:
    """True once a registry snapshot from ``host`` has actually been applied."""
    link = store.read_peer_link(host)
    return link is not None and link.last_registry_at is not None


def test_bridge_mirrors_and_delivers_both_directions(tmp_path: Path) -> None:
    store_a = StateStore(root=tmp_path / "a")
    store_b = StateStore(root=tmp_path / "b")
    _register_live(store_a, "agent-alice", "alice")
    _register_live(store_b, "agent-bob", "bob")

    a2b_r, a2b_w = os.pipe()
    b2a_r, b2a_w = os.pipe()
    bridge_a = Bridge(
        os.fdopen(b2a_r, "rb"),
        os.fdopen(a2b_w, "wb"),
        "hostA",
        store_a,
        poll_interval=0.05,
        sync_interval=0.2,
    )
    bridge_b = Bridge(
        os.fdopen(a2b_r, "rb"),
        os.fdopen(b2a_w, "wb"),
        "hostB",
        store_b,
        poll_interval=0.05,
        sync_interval=0.2,
    )
    ta = threading.Thread(target=bridge_a.run, daemon=True)
    tb = threading.Thread(target=bridge_b.run, daemon=True)
    ta.start()
    tb.start()
    try:
        # Registry mirroring in both directions.
        _wait(lambda: any(a.agent_id == "agent-bob@hostB" for a in store_a.list_agents()))
        _wait(lambda: any(a.agent_id == "agent-alice@hostA" for a in store_b.list_agents()))

        # A -> B: addressed to the qualified id, delivered to the bare local inbox.
        store_a.send_message(from_agent="agent-alice", to_agent="agent-bob@hostB", body="ping")
        _wait(lambda: any(m.body == "ping" for m in store_b.recv_messages("agent-bob")))
        got = store_b.recv_messages("agent-bob")[0]
        assert got.from_agent == "agent-alice@hostA"
        assert got.to_agent == "agent-bob"

        # B -> A reply.
        store_b.send_message(from_agent="agent-bob", to_agent="agent-alice@hostA", body="pong")
        _wait(lambda: any(m.body == "pong" for m in store_a.recv_messages("agent-alice")))
        reply = store_a.recv_messages("agent-alice")[0]
        assert reply.from_agent == "agent-bob@hostB"
        assert reply.to_agent == "agent-alice"
    finally:
        bridge_a.stop()
        bridge_b.stop()
        ta.join(timeout=2)
        tb.join(timeout=2)


def test_bridge_clears_mirrored_agents_on_teardown(tmp_path: Path) -> None:
    store_a = StateStore(root=tmp_path / "a")
    store_b = StateStore(root=tmp_path / "b")
    _register_live(store_b, "agent-bob", "bob")

    a2b_r, a2b_w = os.pipe()
    b2a_r, b2a_w = os.pipe()
    bridge_a = Bridge(
        os.fdopen(b2a_r, "rb"),
        os.fdopen(a2b_w, "wb"),
        "hostA",
        store_a,
        poll_interval=0.05,
        sync_interval=0.2,
    )
    bridge_b = Bridge(
        os.fdopen(a2b_r, "rb"),
        os.fdopen(b2a_w, "wb"),
        "hostB",
        store_b,
        poll_interval=0.05,
        sync_interval=0.2,
    )
    ta = threading.Thread(target=bridge_a.run, daemon=True)
    tb = threading.Thread(target=bridge_b.run, daemon=True)
    ta.start()
    tb.start()
    _wait(lambda: any(a.agent_id == "agent-bob@hostB" for a in store_a.list_agents()))

    # Tear down the bridge; mirrored remote agents must disappear.
    bridge_a.stop()
    bridge_b.stop()
    ta.join(timeout=2)
    tb.join(timeout=2)
    assert not any(a.agent_id == "agent-bob@hostB" for a in store_a.list_agents(include_stale=True))


# ----------------------------------------------- reconnect loop (injected seams)


def _fake_proc() -> subprocess.Popen[bytes]:
    return cast("subprocess.Popen[bytes]", MagicMock())


def test_reconnect_loop_respawns_until_max(tmp_path: Path) -> None:
    store = StateStore(root=tmp_path)
    spawns: list[list[str]] = []
    waits: list[float] = []

    def spawn(argv: list[str]) -> subprocess.Popen[bytes]:
        spawns.append(argv)
        return _fake_proc()

    def run_once(
        proc: subprocess.Popen[bytes], label: str, store_: StateStore, holder: list[Bridge | None]
    ) -> float:
        return 0.0  # never healthy → backoff keeps growing

    rc = reconnect_loop(
        ["peer"],
        "local",
        store,
        max_reconnects=3,
        install_signals=False,
        spawn=spawn,
        run_once=run_once,
        wait=waits.append,
    )
    assert rc == 0
    assert len(spawns) == 3  # respawned up to the cap
    assert len(waits) == 2  # waited between the three attempts
    assert waits[1] > waits[0]  # exponential backoff grew


def test_reconnect_loop_stops_on_shutdown(tmp_path: Path) -> None:
    store = StateStore(root=tmp_path)
    shutdown = threading.Event()
    spawns: list[list[str]] = []

    def spawn(argv: list[str]) -> subprocess.Popen[bytes]:
        spawns.append(argv)
        return _fake_proc()

    def run_once(
        proc: subprocess.Popen[bytes], label: str, store_: StateStore, holder: list[Bridge | None]
    ) -> float:
        shutdown.set()  # simulate SIGTERM landing mid-connection
        return 0.0

    def no_wait(_d: float) -> None:
        raise AssertionError("must not back off after shutdown")

    reconnect_loop(
        ["peer"],
        "local",
        store,
        install_signals=False,
        shutdown=shutdown,
        spawn=spawn,
        run_once=run_once,
        wait=no_wait,
    )
    assert len(spawns) == 1  # broke immediately after the first run


class TestPeerHostValidation:
    """The peer chooses its own host label; we interpolate it into ``Path.glob``.

    A ``*`` there is a wildcard, not a name — ``*@*.jsonl`` matches every
    host-qualified inbox, so a peer claiming ``host: "*"`` is handed mail queued
    for unrelated hosts. The same string is compared as a *literal* suffix in
    three other places (``_strip_peer``, ``sync_remote_agents``,
    ``clear_remote_agents``), so one field is read two different ways.
    """

    @pytest.mark.parametrize(
        "host",
        ["kaladin", "adolin.lan", "host-1", "my_box", "a", "A1.b-c_d.example.com"],
    )
    def test_plausible_hostnames_are_accepted(self, host: str) -> None:
        assert _is_valid_host(host)

    @pytest.mark.parametrize(
        "host",
        [
            "*",  # the leak: glob matches every host-qualified inbox
            "?",  # single-char wildcard
            "[abc]",  # character class
            "a*b",  # embedded wildcard
            "",  # empty -> "*@.jsonl"
            "..",  # path traversal shape
            "a/b",  # separator
            "a b",  # whitespace
            "-lead",  # must start alphanumeric
            "trail-",  # must end alphanumeric
            "x" * 254,  # over the length cap
        ],
    )
    def test_metacharacters_and_malformed_are_rejected(self, host: str) -> None:
        assert not _is_valid_host(host)

    def test_peer_claiming_star_gets_no_third_host_mail(self, tmp_path: Path) -> None:
        """The reproduction this fix exists for, driven end to end.

        Before validation this forwarded both inboxes to a peer entitled to
        neither.
        """
        store = StateStore(root=tmp_path / "local")
        _register_live(store, "agent-local", "local")
        for host in ("thirdhost", "adolin"):
            (store.root / "inboxes" / f"agent-zzz@{host}.jsonl").write_text(
                json.dumps(
                    {
                        "msg_id": f"private-{host}",
                        "from_agent": "agent-local",
                        "to_agent": f"agent-zzz@{host}",
                        "body": f"MAIL PRIVATE TO {host}",
                        "ts": "2026-01-01T00:00:00Z",
                        "in_reply_to": None,
                    }
                )
                + "\n"
            )

        peer_r, peer_w = os.pipe()
        sent = io.BytesIO()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            sent,
            "kaladin",
            store,
            poll_interval=0.05,
            sync_interval=10,
            recv_timeout=60,
        )
        thread = threading.Thread(target=conn.run, daemon=True)
        thread.start()
        with os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "*"}) + "\n").encode())
            peer.flush()
            thread.join(timeout=5.0)

        # Leak assertions FIRST. Ordered deliberately: with the liveness check
        # first, a regression that hangs reddens this test before the leak
        # assertions are ever evaluated, so the test would report the wrong
        # failure and the leak coverage would be silently unreachable.
        frames = [json.loads(x) for x in sent.getvalue().decode().splitlines() if x.strip()]
        bodies = [f["message"]["body"] for f in frames if f.get("kind") == "msg"]
        assert bodies == [], f"forwarded mail to a peer claiming '*': {bodies}"

        kinds = [f.get("kind") for f in frames]
        assert "registry" not in kinds, (
            f"advertised the registry to a peer we refused: {kinds}. The frame carries "
            "agent ids, names, absolute working directories, pids and focus text."
        )

        assert not thread.is_alive(), "bridge should refuse and exit, not hang"
        assert conn.peer_host is None, "an invalid host must never be assigned"

    def test_valid_host_still_connects(self, tmp_path: Path) -> None:
        """Positive control: the rejection above is the validator firing, not the
        harness failing to connect."""
        store = StateStore(root=tmp_path / "local")
        _register_live(store, "agent-local", "local")
        peer_r, peer_w = os.pipe()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            io.BytesIO(),
            "kaladin",
            store,
            poll_interval=0.05,
            sync_interval=10,
            recv_timeout=60,
        )
        thread = threading.Thread(target=conn.run, daemon=True)
        thread.start()
        with os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            peer.flush()
            _wait(lambda: conn.peer_host == "adolin")
            conn.stop()
            thread.join(timeout=5.0)
        assert conn.peer_host == "adolin"

    def test_eof_after_a_valid_hello_still_tears_down_mirrored_entries(
        self, tmp_path: Path
    ) -> None:
        """An EOF after a *valid* hello must still tear down mirrored entries.

        ``_stop`` is set by a plain EOF, not only by a refusal, so it can be set
        here with a perfectly good ``peer_host`` — a peer that says hello, sends
        its registry, then closes. That path has mirrored entries to clean up.

        What this pins is the property, not an explanation of it. Measured as a
        2x2 over the guard's predicate and the ``return``'s placement, exactly
        one combination fails::

            peer_host + inside try   green      _stop + inside try   green
            peer_host + outside try  green      _stop + outside try  RED

        So *either* change alone closes it, and this test does not distinguish
        which — the shipped code keeps both because they fix it by different
        mechanisms: ``peer_host`` makes the branch not apply, inside-``try``
        makes it harmless.

        The race is one the main thread normally wins, so the wait is slowed
        harness-side to make the ordering deterministic rather than lucky.
        """
        store = StateStore(root=tmp_path / "local")
        _register_live(store, "agent-local", "local")

        peer_r, peer_w = os.pipe()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            io.BytesIO(),
            "kaladin",
            store,
            poll_interval=0.05,
            sync_interval=10,
            recv_timeout=60,
        )
        real_wait = conn._hello.wait  # pyright: ignore[reportPrivateUsage]

        def slow_wait(timeout: float | None = None) -> bool:
            result = real_wait(timeout)
            time.sleep(0.3)  # let the reader hit EOF and set _stop before we proceed
            return result

        conn._hello.wait = slow_wait  # type: ignore[method-assign]

        with os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            peer.write(
                (
                    json.dumps(
                        {
                            "kind": "registry",
                            "agents": [
                                {
                                    "agent_id": "agent-remote",
                                    "name": "remote",
                                    "working_dir": "/tmp",
                                    "pid": os.getpid(),
                                    "pid_start": None,
                                    "last_seen": "2026-01-01T00:00:00Z",
                                }
                            ],
                        }
                    )
                    + "\n"
                ).encode()
            )
            peer.flush()
        thread = threading.Thread(target=conn.run, daemon=True)
        thread.start()
        thread.join(timeout=5.0)

        assert not thread.is_alive()
        mirrored = [a.agent_id for a in store.list_agents(include_stale=True) if "@" in a.agent_id]
        assert mirrored == [], f"EOF after a valid hello left mirrored entries behind: {mirrored}"


# ------------------------------------------------- #55: sync must go both ways


def _pair(
    tmp_path: Path, *, sync_interval: float = 0.2
) -> tuple[StateStore, StateStore, Bridge, Bridge]:
    """Two stores wired by two bridges over in-process pipes (A dials, B serves)."""
    store_a = StateStore(root=tmp_path / "a")
    store_b = StateStore(root=tmp_path / "b")
    a2b_r, a2b_w = os.pipe()
    b2a_r, b2a_w = os.pipe()
    bridge_a = Bridge(
        os.fdopen(b2a_r, "rb"),
        os.fdopen(a2b_w, "wb"),
        "hostA",
        store_a,
        role="connect",
        poll_interval=0.05,
        sync_interval=sync_interval,
    )
    bridge_b = Bridge(
        os.fdopen(a2b_r, "rb"),
        os.fdopen(b2a_w, "wb"),
        "hostB",
        store_b,
        role="serve",
        poll_interval=0.05,
        sync_interval=sync_interval,
    )
    return store_a, store_b, bridge_a, bridge_b


@contextlib.contextmanager
def _running(*bridges: Bridge) -> Generator[None, None, None]:
    threads = [threading.Thread(target=b.run, daemon=True) for b in bridges]
    for t in threads:
        t.start()
    try:
        yield
    finally:
        for b in bridges:
            b.stop()
        for t in threads:
            t.join(timeout=3)


class TestBidirectionalRegistrySync:
    """The primary #55 defect: the initiator never learned the peer's agents.

    The pre-existing ``test_bridge_mirrors_and_delivers_both_directions`` asserts
    the mirror rows exist. These assert the thing the reporter actually could not
    do — *address* a peer's agent — through the resolver, from both ends, in all
    three address forms.
    """

    def test_the_initiator_can_address_the_peer_by_qualified_id(self, tmp_path: Path) -> None:
        store_a, store_b, bridge_a, bridge_b = _pair(tmp_path)
        _register_live(store_a, "agent-alice", "alice")
        _register_live(store_b, "agent-bob", "bob")
        with _running(bridge_a, bridge_b):
            _wait(lambda: any(a.agent_id == "agent-bob@hostB" for a in store_a.list_agents()))
            msg = store_a.send_message(
                from_agent="agent-alice", to_agent="agent-bob@hostB", body="ping"
            )
            assert msg.to_agent == "agent-bob@hostB"

    def test_the_initiator_can_address_the_peer_by_display_name(self, tmp_path: Path) -> None:
        store_a, store_b, bridge_a, bridge_b = _pair(tmp_path)
        _register_live(store_a, "agent-alice", "alice")
        _register_live(store_b, "agent-bob", "bob")
        with _running(bridge_a, bridge_b):
            _wait(lambda: any(a.agent_id == "agent-bob@hostB" for a in store_a.list_agents()))
            msg = store_a.send_message(from_agent="agent-alice", to_agent="bob", body="ping")
            assert msg.to_agent == "agent-bob@hostB"

    def test_the_initiator_can_still_address_a_bare_local_id(self, tmp_path: Path) -> None:
        store_a, store_b, bridge_a, bridge_b = _pair(tmp_path)
        _register_live(store_a, "agent-alice", "alice")
        _register_live(store_a, "agent-local", "local")
        _register_live(store_b, "agent-bob", "bob")
        with _running(bridge_a, bridge_b):
            _wait(lambda: any(a.agent_id == "agent-bob@hostB" for a in store_a.list_agents()))
            msg = store_a.send_message(
                from_agent="agent-alice", to_agent="agent-local", body="ping"
            )
            assert msg.to_agent == "agent-local"

    def test_the_serving_end_can_address_the_initiator_the_same_three_ways(
        self, tmp_path: Path
    ) -> None:
        store_a, store_b, bridge_a, bridge_b = _pair(tmp_path)
        _register_live(store_a, "agent-alice", "alice")
        _register_live(store_b, "agent-bob", "bob")
        with _running(bridge_a, bridge_b):
            _wait(lambda: any(a.agent_id == "agent-alice@hostA" for a in store_b.list_agents()))
            by_id = store_b.send_message(
                from_agent="agent-bob", to_agent="agent-alice@hostA", body="1"
            )
            by_name = store_b.send_message(from_agent="agent-bob", to_agent="alice", body="2")
            bare = store_b.send_message(from_agent="agent-bob", to_agent="agent-bob", body="3")
            assert by_id.to_agent == "agent-alice@hostA"
            assert by_name.to_agent == "agent-alice@hostA"
            assert bare.to_agent == "agent-bob"

    def test_both_ends_record_a_peer_link_with_sync_stats(self, tmp_path: Path) -> None:
        store_a, store_b, bridge_a, bridge_b = _pair(tmp_path)
        _register_live(store_a, "agent-alice", "alice")
        _register_live(store_b, "agent-bob", "bob")
        with _running(bridge_a, bridge_b):
            _wait(lambda: any(a.agent_id == "agent-bob@hostB" for a in store_a.list_agents()))
            _wait(lambda: any(a.agent_id == "agent-alice@hostA" for a in store_b.list_agents()))
            link_a = store_a.read_peer_link("hostB")
            link_b = store_b.read_peer_link("hostA")
            assert link_a is not None and link_b is not None
            assert link_a.role == "connect"
            assert link_b.role == "serve"
            assert link_a.last_registry_at is not None
            assert link_b.last_registry_at is not None
            assert link_a.last_registry_agents == 1
            assert link_b.last_registry_agents == 1
            # The peer reports the counts BEHIND its list, so "advertised zero"
            # can be told apart from "never answered" on the receiving end.
            assert link_a.peer_registry_rows == 1
            assert link_a.peer_stale_rows == 0

    def test_a_peer_with_only_dead_agents_advertises_zero_and_says_why(
        self, tmp_path: Path
    ) -> None:
        """The state that reads as a broken bridge but is a fault on the peer."""
        store_a, store_b, bridge_a, bridge_b = _pair(tmp_path)
        _register_live(store_a, "agent-alice", "alice")
        store_b.register_agent(
            name="bob", working_dir="/tmp", pid=_dead_pid(), agent_id="agent-bob"
        )
        with _running(bridge_a, bridge_b):
            _wait(lambda: _synced(store_a, "hostB"))
            link = store_a.read_peer_link("hostB")
            assert link is not None
            assert link.last_registry_agents == 0
            assert link.peer_registry_rows == 1
            assert link.peer_stale_rows == 1
            with pytest.raises(ValueError) as excinfo:
                store_a.send_message(from_agent="agent-alice", to_agent="agent-bob@hostB", body="x")
            text = str(excinfo.value)
            assert "that snapshot advertised ZERO AGENTS" in text
            assert "the fix belongs on 'hostB'" in text

    def test_teardown_keeps_the_record_and_marks_it_detached(self, tmp_path: Path) -> None:
        store_a, store_b, bridge_a, bridge_b = _pair(tmp_path)
        _register_live(store_a, "agent-alice", "alice")
        _register_live(store_b, "agent-bob", "bob")
        with _running(bridge_a, bridge_b):
            _wait(lambda: any(a.agent_id == "agent-bob@hostB" for a in store_a.list_agents()))
        link = store_a.read_peer_link("hostB")
        assert link is not None, "the record must survive teardown — its absence is a diagnosis"
        assert link.detached_at is not None


class TestRegistryPull:
    """The pull half of sync, and what it makes observable."""

    def _fake_peer(
        self, tmp_path: Path, *, sync_interval: float = 0.2
    ) -> tuple[StateStore, Bridge, io.BytesIO, int]:
        """A bridge whose peer is a raw pipe we drive by hand."""
        store = StateStore(root=tmp_path / "local")
        _register_live(store, "agent-local", "local")
        peer_r, peer_w = os.pipe()
        sent = io.BytesIO()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            sent,
            "kaladin",
            store,
            role="connect",
            poll_interval=0.05,
            sync_interval=sync_interval,
            recv_timeout=60,
        )
        return store, conn, sent, peer_w

    @staticmethod
    def _frames(sent: io.BytesIO) -> list[dict[str, object]]:
        return [json.loads(x) for x in sent.getvalue().decode().splitlines() if x.strip()]

    def test_a_registry_request_is_sent_right_after_the_handshake(self, tmp_path: Path) -> None:
        store, conn, sent, peer_w = self._fake_peer(tmp_path)
        with _running(conn), os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            peer.flush()
            _wait(lambda: any(f.get("kind") == "registry-request" for f in self._frames(sent)))
        link = store.read_peer_link("adolin")
        assert link is not None
        assert link.registry_requests_sent >= 1

    def test_a_peers_registry_request_is_answered_with_a_registry(self, tmp_path: Path) -> None:
        _store, conn, sent, peer_w = self._fake_peer(tmp_path, sync_interval=30)
        with _running(conn), os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            peer.flush()
            _wait(lambda: sum(f.get("kind") == "registry" for f in self._frames(sent)) == 1)
            peer.write((json.dumps({"kind": "registry-request"}) + "\n").encode())
            peer.flush()
            # sync_interval is 30s, so a second registry frame can only be the
            # answer to the pull — not the periodic push.
            _wait(lambda: sum(f.get("kind") == "registry" for f in self._frames(sent)) == 2)

    def test_the_registry_frame_carries_the_counts_behind_the_list(self, tmp_path: Path) -> None:
        store, conn, sent, peer_w = self._fake_peer(tmp_path, sync_interval=30)
        store.register_agent(
            name="gone", working_dir="/tmp", pid=_dead_pid(), agent_id="agent-gone"
        )
        with _running(conn), os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            peer.flush()
            _wait(lambda: any(f.get("kind") == "registry" for f in self._frames(sent)))
        frame = next(f for f in self._frames(sent) if f.get("kind") == "registry")
        assert frame["local_rows"] == 2
        assert frame["stale_local_rows"] == 1
        assert [a["agent_id"] for a in frame["agents"]] == ["agent-local"]  # type: ignore[index]

    def test_a_bridge_that_never_syncs_is_visibly_different_from_one_that_has(
        self, tmp_path: Path
    ) -> None:
        """The whole diagnosis, in one test: same pipe, same pings, two states."""
        store, conn, _sent, peer_w = self._fake_peer(tmp_path)
        with _running(conn), os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            peer.write((json.dumps({"kind": "ping"}) + "\n").encode())
            peer.flush()
            _wait(lambda: store.read_peer_link("adolin") is not None)
            silent = store.read_peer_link("adolin")
            assert silent is not None
            assert silent.last_registry_at is None, "no registry was ever sent"
            assert silent.last_frame_at is not None, "but frames ARE arriving"
            with pytest.raises(ValueError) as excinfo:
                store.send_message(from_agent="agent-local", to_agent="agent-r@adolin", body="x")
            assert "has NEVER RECEIVED A REGISTRY SNAPSHOT from 'adolin'" in str(excinfo.value)

            # Now let the peer answer, and the same address resolves.
            peer.write(
                (
                    json.dumps(
                        {
                            "kind": "registry",
                            "agents": [
                                {
                                    "agent_id": "agent-r",
                                    "name": "remote",
                                    "working_dir": "/tmp",
                                    "pid": os.getpid(),
                                    "pid_start": None,
                                    "last_seen": "2026-01-01T00:00:00Z",
                                }
                            ],
                            "local_rows": 1,
                            "stale_local_rows": 0,
                        }
                    )
                    + "\n"
                ).encode()
            )
            peer.flush()
            _wait(lambda: _synced(store, "adolin"))
            msg = store.send_message(from_agent="agent-local", to_agent="agent-r@adolin", body="x")
            assert msg.to_agent == "agent-r@adolin"

    def test_an_old_peer_that_omits_the_counts_reads_as_unknown_not_zero(
        self, tmp_path: Path
    ) -> None:
        """Version skew: a pre-#55 peer sends `agents` and nothing else."""
        store, conn, _sent, peer_w = self._fake_peer(tmp_path)
        with _running(conn), os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            peer.write((json.dumps({"kind": "registry", "agents": []}) + "\n").encode())
            peer.flush()
            _wait(lambda: _synced(store, "adolin"))
        link = store.read_peer_link("adolin")
        assert link is not None
        assert link.last_registry_agents == 0
        assert link.peer_registry_rows is None, "absent must not be reported as zero"


class TestReaderSurvivesABadFrame:
    """A frame this version cannot model used to kill the reader thread silently.

    The bridge then kept forwarding mail (main thread) while ingesting nothing,
    which is one of the ways #55's symptom is produced with everything else
    looking healthy.
    """

    def test_a_malformed_registry_does_not_stop_later_delivery(self, tmp_path: Path) -> None:
        store = StateStore(root=tmp_path / "local")
        _register_live(store, "agent-local", "local")
        peer_r, peer_w = os.pipe()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            io.BytesIO(),
            "kaladin",
            store,
            poll_interval=0.05,
            sync_interval=30,
            recv_timeout=60,
        )
        with _running(conn), os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            # An Agent record this version cannot validate (no `name`).
            peer.write(
                (
                    json.dumps({"kind": "registry", "agents": [{"agent_id": "agent-r"}]}) + "\n"
                ).encode()
            )
            peer.write(
                (
                    json.dumps(
                        {
                            "kind": "msg",
                            "message": {
                                "msg_id": "m1",
                                "from_agent": "agent-r@adolin",
                                "to_agent": "agent-local",
                                "body": "still alive",
                                "ts": "2026-01-01T00:00:00Z",
                                "in_reply_to": None,
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            peer.flush()
            _wait(lambda: any(m.body == "still alive" for m in store.recv_messages("agent-local")))
            link = store.read_peer_link("adolin")
            assert link is not None
            assert link.note is not None
            assert "could not be processed and was DROPPED" in link.note

    def test_an_unknown_frame_kind_is_ignored_and_leaves_no_note(self, tmp_path: Path) -> None:
        """A receiver must shrug at a `kind` it does not know.

        This is the forward-compatibility half that had no test. Its dangerous
        sibling above -- a frame the reader cannot model killing the reader,
        after which mail keeps flowing while nothing is ingested -- is pinned,
        and that was #55's failure mode. Nothing pinned this one, so the next
        edit to the reader's if/elif chain could turn an unknown kind into a
        crash, or into a DROPPED note, with no test going red.

        The distinction matters: a frame from a newer peer is not an error and
        must not be recorded as one, or every mixed-version bridge accumulates
        notes about working correctly. The bridge carries no protocol version,
        so compatibility between hosts is structural -- and a structural
        guarantee with no test is a coincidence. Found by the cross-repo review
        of #56.
        """
        store = StateStore(root=tmp_path / "local")
        _register_live(store, "agent-local", "local")
        peer_r, peer_w = os.pipe()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            io.BytesIO(),
            "kaladin",
            store,
            poll_interval=0.05,
            sync_interval=30,
            recv_timeout=60,
        )
        with _running(conn), os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            # A frame from a version that does not exist yet.
            peer.write(
                (
                    json.dumps(
                        {"kind": "a-frame-from-the-future", "payload": {"anything": [1, 2, 3]}}
                    )
                    + "\n"
                ).encode()
            )
            peer.write(
                (
                    json.dumps(
                        {
                            "kind": "msg",
                            "message": {
                                "msg_id": "m1",
                                "from_agent": "agent-r@adolin",
                                "to_agent": "agent-local",
                                "body": "still delivering",
                                "ts": "2026-01-01T00:00:00Z",
                                "in_reply_to": None,
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            peer.flush()
            _wait(
                lambda: any(
                    m.body == "still delivering" for m in store.recv_messages("agent-local")
                )
            )
            link = store.read_peer_link("adolin")
            assert link is not None
            # Ignored, NOT dropped-with-a-note: an unknown kind is a newer peer,
            # not a fault, and recording it as one would make every
            # mixed-version bridge look broken.
            assert link.note is None

    def test_non_json_noise_before_the_hello_is_recorded_not_dropped(self, tmp_path: Path) -> None:
        """A login banner on the far side is a leading cause of a dead handshake."""
        store = StateStore(root=tmp_path / "local")
        _register_live(store, "agent-local", "local")
        peer_r, peer_w = os.pipe()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            io.BytesIO(),
            "kaladin",
            store,
            poll_interval=0.05,
            sync_interval=30,
            recv_timeout=60,
        )
        with _running(conn), os.fdopen(peer_w, "wb") as peer:
            peer.write(b"Welcome to Ubuntu 24.04 LTS\n")
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            peer.flush()
            _wait(lambda: store.read_peer_link("adolin") is not None)
            link = store.read_peer_link("adolin")
            assert link is not None
            assert link.note is not None
            assert "not a JSON bus frame" in link.note
            assert "Welcome to Ubuntu" in link.note


def test_a_peer_frame_addressed_outside_the_state_root_writes_nothing(tmp_path: Path) -> None:
    """#12's end-to-end reproduction: a `msg` frame to `../../escaped`.

    Before validation this created `<state_root>/../escaped.jsonl`. The frame
    must be refused, and the reader must survive it -- a frame that kills the
    reader is the #55 failure, so a later valid frame still has to land.
    """
    store = StateStore(root=tmp_path / "local")
    _register_live(store, "agent-local", "local")

    def frame(msg_id: str, to_agent: str) -> bytes:
        msg = {
            "msg_id": msg_id,
            "from_agent": "agent-peer@adolin",
            "to_agent": to_agent,
            "body": "x",
            "ts": "2026-01-01T00:00:00Z",
            "in_reply_to": None,
        }
        return (json.dumps({"kind": "msg", "message": msg}) + "\n").encode()

    peer_r, peer_w = os.pipe()
    conn = Bridge(
        os.fdopen(peer_r, "rb"),
        io.BytesIO(),
        "kaladin",
        store,
        poll_interval=0.05,
        sync_interval=10,
        recv_timeout=60,
    )
    thread = threading.Thread(target=conn.run, daemon=True)
    thread.start()
    with os.fdopen(peer_w, "wb") as peer:
        peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
        peer.write(frame("evil", "../../escaped"))
        peer.write(frame("good", "agent-local"))
        peer.flush()
        _wait(lambda: any(m.msg_id == "good" for m in store.recv_messages("agent-local")))
    thread.join(timeout=5.0)

    assert list(tmp_path.rglob("escaped*")) == [], "a peer frame wrote outside the state root"
    assert [m.msg_id for m in store.recv_messages("agent-local")] == ["good"]


def _unhealthy_run(
    proc: subprocess.Popen[bytes], label: str, store: StateStore, holder: list[Bridge | None]
) -> float:
    return 0.0


class TestConnectionStatusLines:
    """`conjoin` prints connect / disconnect / retry to its terminal.

    The owner watches that terminal. Before this, a dropped link was visible
    only by running `spanreed list` and finding the peer DETACHED.
    """

    @staticmethod
    def _lines(err: str) -> list[str]:
        return [
            ln.split("spanreed conjoin: ", 1)[1]
            for ln in err.splitlines()
            if "spanreed conjoin: " in ln
        ]

    def test_the_reconnect_loop_announces_each_attempt_and_the_wait(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        reconnect_loop(
            ["peer"],
            "local",
            StateStore(root=tmp_path),
            max_reconnects=2,
            install_signals=False,
            spawn=lambda _argv: _fake_proc(),
            run_once=_unhealthy_run,
            wait=lambda _d: None,
            peer="adolin",
        )
        lines = self._lines(capsys.readouterr().err)
        assert lines[0] == "connecting to adolin..."
        assert lines[1].startswith("retrying in ")
        assert lines[2] == "reconnecting to adolin..."
        assert lines[-2] == "giving up after 2 attempt(s) (--max-reconnects)"
        assert lines[-1] == "stopped"

    def test_a_bridge_says_who_it_connected_to_and_why_it_disconnected(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        store = StateStore(root=tmp_path / "local")
        peer_r, peer_w = os.pipe()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            io.BytesIO(),
            "kaladin",
            store,
            role="connect",
            poll_interval=0.05,
            sync_interval=10,
            recv_timeout=60,
        )
        thread = threading.Thread(target=conn.run, daemon=True)
        thread.start()
        with os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            peer.flush()
            _wait(lambda: conn.peer_host == "adolin")
        thread.join(timeout=5.0)  # closing the pipe is the disconnect

        lines = self._lines(capsys.readouterr().err)
        assert "connected to adolin" in lines
        disconnect = [ln for ln in lines if ln.startswith("disconnected from adolin after ")]
        assert len(disconnect) == 1 and disconnect[0].endswith(": the pipe closed"), lines

    def test_the_first_cause_wins_over_the_eof_that_follows_it(self, tmp_path: Path) -> None:
        # stop() names the cause; the reader's EOF lands afterwards and must not
        # overwrite "shutting down" with "the pipe closed".
        conn = Bridge(io.BytesIO(), io.BytesIO(), "kaladin", StateStore(root=tmp_path))
        conn.stop()
        conn._end("the pipe closed")  # pyright: ignore[reportPrivateUsage]
        assert conn._end_reason == "shutting down"  # pyright: ignore[reportPrivateUsage]

    def test_the_serve_end_prints_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # ssh forwards the serve end's stderr to the connect terminal, so a serve
        # line would print every event twice.
        store = StateStore(root=tmp_path / "remote")
        peer_r, peer_w = os.pipe()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            io.BytesIO(),
            "adolin",
            store,
            role="serve",
            poll_interval=0.05,
            sync_interval=10,
            recv_timeout=60,
        )
        thread = threading.Thread(target=conn.run, daemon=True)
        thread.start()
        with os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "kaladin"}) + "\n").encode())
            peer.flush()
            _wait(lambda: conn.peer_host == "kaladin")
        thread.join(timeout=5.0)
        assert self._lines(capsys.readouterr().err) == []

    # The three reasons the review found untested: removing any of them left
    # the suite green. Each test below drives the real Bridge into that state.

    def _connect_bridge(self, tmp_path: Path, **kw: float) -> tuple[Bridge, int]:
        peer_r, peer_w = os.pipe()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            io.BytesIO(),
            "kaladin",
            StateStore(root=tmp_path / "local"),
            role="connect",
            poll_interval=0.05,
            sync_interval=10,
            **kw,
        )
        return conn, peer_w

    def test_the_watchdog_reason_names_the_silence(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The headline docs case: the peer says hello, then goes quiet with the
        # pipe still OPEN, so only the watchdog can end the link.
        conn, peer_w = self._connect_bridge(tmp_path, recv_timeout=0.3)
        thread = threading.Thread(target=conn.run, daemon=True)
        thread.start()
        with os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            peer.flush()
            thread.join(timeout=5.0)  # pipe still open here: only the watchdog ends it
            assert not thread.is_alive()
        lines = self._lines(capsys.readouterr().err)
        disconnect = [ln for ln in lines if ln.startswith("disconnected from adolin")]
        assert disconnect and disconnect[0].endswith(": no frame from the peer for 0s"), lines

    def test_no_handshake_says_could_not_connect(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        conn, peer_w = self._connect_bridge(tmp_path, hello_timeout=0.2)
        with os.fdopen(peer_w, "wb"):  # open, but never sends a hello
            conn.run()
        lines = self._lines(capsys.readouterr().err)
        assert any(
            ln.startswith("could not connect: the peer sent no handshake within") for ln in lines
        ), lines
        assert not any(ln.startswith("connected to") for ln in lines)

    def test_no_handshake_is_silent_on_the_serve_end(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # The connect-only gate on the no-handshake line, pinned separately.
        peer_r, peer_w = os.pipe()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            io.BytesIO(),
            "adolin",
            StateStore(root=tmp_path / "remote"),
            role="serve",
            hello_timeout=0.2,
        )
        with os.fdopen(peer_w, "wb"):
            conn.run()
        assert self._lines(capsys.readouterr().err) == []

    def test_a_refused_host_label_is_the_reason(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        conn, peer_w = self._connect_bridge(tmp_path, recv_timeout=60)
        thread = threading.Thread(target=conn.run, daemon=True)
        thread.start()
        with os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "*"}) + "\n").encode())
            peer.flush()
            thread.join(timeout=5.0)
        lines = self._lines(capsys.readouterr().err)
        disconnect = [ln for ln in lines if ln.startswith("disconnected from the peer")]
        assert disconnect and disconnect[0].endswith(
            ": refused the peer's invalid host label '*'"
        ), lines
        assert not any(ln.startswith("connected to") for ln in lines)


class TestConfigurableTimeouts:
    """`--timeout` must reach all three timers that can end a link.

    The bridge's watchdog, ssh's ServerAlive keepalive, and the FAR end's
    watchdog each tear a link down independently. A knob that changed only
    one would look like it did nothing: ssh, or the far end, would still drop
    the pipe at the old ~15s.
    """

    @staticmethod
    def _capture_connect(
        monkeypatch: pytest.MonkeyPatch, **kw: float | None
    ) -> tuple[list[str], dict[str, float]]:
        seen: dict[str, object] = {}
        bridge_kwargs: dict[str, float] = {}

        def fake_loop(argv: list[str], label: str, store: StateStore, **loop_kw: object) -> int:
            seen["argv"] = argv
            # Drive the real run_once connect() built, so its timeouts are what
            # reach the Bridge, not what a test assumed.
            run_once = cast("Callable[..., float]", loop_kw["run_once"])
            run_once(_fake_proc(), label, store, [None])
            return 0

        def fake_once(*_a: object, **kwargs: float) -> float:
            bridge_kwargs.update(kwargs)
            return 0.0

        monkeypatch.setattr(bridge, "reconnect_loop", fake_loop)
        monkeypatch.setattr(bridge, "_run_bridge_once", fake_once)

        def probe(host: str, timeout: float = 20.0) -> str:
            return "/r/spanreed"

        monkeypatch.setattr(bridge, "_probe_remote_spanreed", probe)
        bridge.connect("adolin", self_host="kaladin", **kw)  # type: ignore[arg-type]
        return cast("list[str]", seen["argv"]), bridge_kwargs

    def test_the_default_is_unchanged_and_sends_no_flag_to_the_peer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv, kwargs = self._capture_connect(monkeypatch)
        assert "ServerAliveInterval=5" in argv and "ServerAliveCountMax=3" in argv
        # Not forwarded when unset: an older peer's `conjoin --serve` rejects it.
        assert argv[-1] == "/r/spanreed conjoin --serve"
        assert kwargs == {"recv_timeout": 15.0, "hello_timeout": 15.0}

    def test_a_raised_timeout_reaches_watchdog_ssh_and_the_far_end(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        argv, kwargs = self._capture_connect(monkeypatch, timeout=60.0)
        assert "ServerAliveInterval=20" in argv and "ServerAliveCountMax=3" in argv  # ~60s
        assert argv[-1] == "/r/spanreed conjoin --serve --timeout 60"
        assert kwargs["recv_timeout"] == 60.0

    def test_the_handshake_timeout_reaches_the_bridge_and_the_probe(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        probe_timeouts: list[float] = []

        def probe(host: str, timeout: float = 20.0) -> str:
            probe_timeouts.append(timeout)
            return "/r/spanreed"

        def no_loop(argv: list[str], *_args: object, **_kwargs: object) -> int:
            return 0

        monkeypatch.setattr(bridge, "_probe_remote_spanreed", probe)
        monkeypatch.setattr(bridge, "reconnect_loop", no_loop)
        bridge.connect("adolin", self_host="kaladin", handshake_timeout=45.0)
        assert probe_timeouts == [45.0]

        _, kwargs = self._capture_connect(monkeypatch, handshake_timeout=45.0)
        assert kwargs["hello_timeout"] == 45.0

    def test_the_serve_end_applies_the_forwarded_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made: list[float] = []

        class Recorder:
            def __init__(self, *_a: object, recv_timeout: float, **_k: object) -> None:
                made.append(recv_timeout)

            def run(self) -> float:
                return 0.0

        monkeypatch.setattr(bridge, "Bridge", Recorder)
        bridge.serve(self_host="adolin", timeout=60.0)
        bridge.serve(self_host="adolin")
        assert made == [60.0, 15.0]

    @staticmethod
    def _stub_entry_points(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, object]]]:
        # Stubbed so a regression in the range check fails fast instead of
        # really ssh'ing to "adolin" and hanging the suite.
        calls: list[tuple[str, dict[str, object]]] = []

        def fake_connect(host: str, **kw: object) -> int:
            calls.append(("connect", kw))
            return 0

        def fake_serve(**kw: object) -> int:
            calls.append(("serve", kw))
            return 0

        monkeypatch.setattr(bridge, "connect", fake_connect)
        monkeypatch.setattr(bridge, "serve", fake_serve)
        return calls

    @pytest.mark.parametrize("flag", ["--timeout", "--handshake-timeout"])
    @pytest.mark.parametrize("value", ["4", "nan", "inf", "-inf", "1e300"])
    @pytest.mark.parametrize("end", [["adolin"], ["--serve"]])
    def test_the_cli_refuses_out_of_range_and_non_finite_timeouts(
        self,
        flag: str,
        value: str,
        end: list[str],
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # nan is the dangerous one: it compares false against everything, so a
        # `value < 6` check let it through, and on the serve end a NaN watchdog
        # never fires. inf and 1e300 crashed later in round() / Event.wait.
        from spanreed import cli

        calls = self._stub_entry_points(monkeypatch)
        # `--flag=value`, so argparse cannot mistake `-inf` for an option and
        # refuse it before the range check -- that form is the one that reaches it.
        assert cli.main(["conjoin", *end, f"{flag}={value}"]) == 2
        assert "must be between 6 and 86400 seconds" in capsys.readouterr().err
        assert calls == [], "an invalid timeout reached connect/serve"

    def test_the_cli_forwards_serve_timeout_to_serve(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The far end's half of the wiring, through cli.main. Without it the
        # connect end forwards `--serve --timeout 60` and the far end drops it.
        from spanreed import cli

        calls = self._stub_entry_points(monkeypatch)
        assert cli.main(["conjoin", "--serve", "--timeout", "60", "--label", "adolin"]) == 0
        assert calls == [("serve", {"self_host": "adolin", "timeout": 60.0})]

    def test_the_cli_forwards_both_timeouts_to_connect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from spanreed import cli

        calls = self._stub_entry_points(monkeypatch)
        cli.main(["conjoin", "adolin", "--timeout", "60", "--handshake-timeout", "45"])
        assert calls[0][0] == "connect"
        assert calls[0][1]["timeout"] == 60.0 and calls[0][1]["handshake_timeout"] == 45.0

    def test_the_watchdog_really_fires_at_the_configured_silence(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # End to end through _run_bridge_once's own Bridge construction: a hello,
        # then an OPEN but silent pipe. The configured value is what fires.
        peer_r, peer_w = os.pipe()
        proc = cast(
            "subprocess.Popen[bytes]",
            SimpleNamespace(
                stdout=os.fdopen(peer_r, "rb"),
                stdin=io.BytesIO(),
                terminate=lambda: None,
                wait=lambda timeout=None: 0,  # pyright: ignore[reportUnknownLambdaType]
                kill=lambda: None,
            ),
        )
        # On its own thread with a bounded join: called directly, a broken
        # watchdog never returns and the whole suite HANGS here instead of
        # failing (#78 review). The pipe stays open until after the join, so
        # only the watchdog can end the run.
        holder: list[Bridge | None] = [None]
        runner = threading.Thread(
            target=lambda: bridge._run_bridge_once(  # pyright: ignore[reportPrivateUsage]
                proc, "kaladin", StateStore(root=tmp_path), holder, recv_timeout=0.4
            ),
            daemon=True,
        )
        with os.fdopen(peer_w, "wb") as peer:
            peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
            peer.flush()
            started = time.monotonic()
            runner.start()
            runner.join(timeout=5.0)
            elapsed = time.monotonic() - started
            fired = not runner.is_alive()
            if not fired and holder[0] is not None:
                holder[0].stop()  # let the daemon thread exit rather than leak
        assert fired, "the watchdog never fired: a silent, open pipe kept the bridge up"
        assert 0.4 <= elapsed < 3.0, elapsed
        assert "no frame from the peer for 0s" in capsys.readouterr().err


class TestBridgeSteadyState:
    """What the bridge does REPEATEDLY, not just in its first second (#43).

    Re-measured on main before this class existed: of the issue's five one-line
    mutations, only the watchdog-disabled one was caught (by #72/#74's tests).
    These four still left all 676 tests green:

    1. `_last_recv` never refreshed -> every healthy bridge kills itself after
       recv_timeout, forever, with CI green.
    3. the outbound cursor never advances -> the whole inbox is re-forwarded
       every poll.
    4. a saved cursor is ignored -> a restart re-sends everything.
    5. mirrored entries advertised back -> unbounded registry growth.
    """

    @staticmethod
    def _bridge(
        tmp_path: Path, **kw: float
    ) -> tuple[Bridge, StateStore, io.BytesIO, int, threading.Thread]:
        store = StateStore(root=tmp_path / "local")
        sink = io.BytesIO()
        peer_r, peer_w = os.pipe()
        conn = Bridge(
            os.fdopen(peer_r, "rb"),
            sink,
            "kaladin",
            store,
            poll_interval=0.05,
            sync_interval=kw.pop("sync_interval", 10),
            recv_timeout=kw.pop("recv_timeout", 60),
        )
        return conn, store, sink, peer_w, threading.Thread(target=conn.run, daemon=True)

    @staticmethod
    def _frames(sink: io.BytesIO, kind: str) -> list[dict[str, object]]:
        lines = sink.getvalue().decode().splitlines()
        return [f for f in (json.loads(x) for x in lines if x.strip()) if f.get("kind") == kind]

    @staticmethod
    def _queue(store: StateStore, msg_id: str) -> None:
        # Mail queued for a remote agent: it sits in `<id>@adolin.jsonl`, the
        # outbound queue the bridge tails.
        (store.root / "inboxes" / "agent-r@adolin.jsonl").open("a").write(
            json.dumps(
                {
                    "msg_id": msg_id,
                    "from_agent": "agent-local",
                    "to_agent": "agent-r@adolin",
                    "body": msg_id,
                    "ts": "2026-01-01T00:00:00Z",
                    "in_reply_to": None,
                }
            )
            + "\n"
        )

    @staticmethod
    def _hello(peer: IO[bytes]) -> None:
        peer.write((json.dumps({"kind": "hello", "host": "adolin"}) + "\n").encode())
        peer.flush()

    def test_the_cursor_advances_and_a_message_is_sent_once(self, tmp_path: Path) -> None:
        # Kills mutation 3. Ten-plus poll intervals: without the cursor the
        # message would be re-emitted on every one of them.
        _conn, store, sink, peer_w, thread = self._bridge(tmp_path)
        self._queue(store, "m1")
        thread.start()
        with os.fdopen(peer_w, "wb") as peer:
            self._hello(peer)
            _wait(lambda: bool(self._frames(sink, "msg")))
            time.sleep(0.6)
        thread.join(timeout=5.0)
        assert [f["message"]["msg_id"] for f in self._frames(sink, "msg")] == ["m1"]  # type: ignore[index]
        assert store.get_cursor(".bridge.agent-r@adolin") == "m1"

    def test_a_saved_cursor_is_honoured_on_restart(self, tmp_path: Path) -> None:
        # Kills mutation 4: protocol.md promises a restart resumes without
        # re-sending.
        _conn, store, sink, peer_w, thread = self._bridge(tmp_path)
        self._queue(store, "already-sent")
        self._queue(store, "new")
        store.set_cursor(".bridge.agent-r@adolin", "already-sent")
        thread.start()
        with os.fdopen(peer_w, "wb") as peer:
            self._hello(peer)
            _wait(lambda: bool(self._frames(sink, "msg")))
            time.sleep(0.3)
        thread.join(timeout=5.0)
        assert [f["message"]["msg_id"] for f in self._frames(sink, "msg")] == ["new"]  # type: ignore[index]

    def test_a_live_peer_keeps_the_link_up_past_the_watchdog(self, tmp_path: Path) -> None:
        # Kills mutation 1, the one that matters most, and the positive control
        # for the watchdog-fires tests: a guard that can only fire is as broken
        # as one that never can. A peer that sends nothing but pings for four
        # watchdog windows must not be torn down.
        # 0.5s watchdog against pings 0.1s apart: five pings of margin per
        # window, so a loaded machine stalling for a few hundred ms cannot fail
        # it, while a watchdog that is never refreshed still fires inside 0.5s.
        conn, _store, _sink, peer_w, thread = self._bridge(tmp_path, recv_timeout=0.5)
        thread.start()
        with os.fdopen(peer_w, "wb") as peer:
            self._hello(peer)
            for _ in range(15):  # 1.5s of pings = three watchdog windows
                peer.write((json.dumps({"kind": "ping"}) + "\n").encode())
                peer.flush()
                time.sleep(0.1)
            assert thread.is_alive(), "the watchdog tore down a link that was pinging"
        thread.join(timeout=5.0)
        assert conn._end_reason == "the pipe closed"  # pyright: ignore[reportPrivateUsage]

    def test_mirrored_agents_are_never_advertised_back(self, tmp_path: Path) -> None:
        # Kills mutation 5: re-advertising `agent-r@adolin` to adolin makes it
        # `agent-r@adolin@kaladin` there, then `...@adolin` here, one @host per
        # sync round, forever.
        _conn, store, sink, peer_w, thread = self._bridge(tmp_path)
        _register_live(store, "agent-local", "local")
        remote = {
            "agent_id": "agent-r",
            "name": "r",
            "working_dir": "/r",
            "pid": os.getpid(),
            "pid_start": None,
            "last_seen": "2026-01-01T00:00:00Z",
        }
        thread.start()
        with os.fdopen(peer_w, "wb") as peer:
            self._hello(peer)
            peer.write((json.dumps({"kind": "registry", "agents": [remote]}) + "\n").encode())
            peer.flush()
            _wait(lambda: any(a.agent_id == "agent-r@adolin" for a in store.list_agents()))
            # Ask for a fresh advertisement AFTER the mirror exists.
            before = len(self._frames(sink, "registry"))
            peer.write((json.dumps({"kind": "registry-request"}) + "\n").encode())
            peer.flush()
            _wait(lambda: len(self._frames(sink, "registry")) > before)
        thread.join(timeout=5.0)
        advertised = [
            cast("dict[str, object]", a)["agent_id"]
            for f in self._frames(sink, "registry")
            for a in cast("list[object]", f["agents"])
        ]
        assert "agent-local" in advertised, advertised  # the control: it advertises at all
        assert not [i for i in advertised if "@" in str(i)], advertised


def test_a_registry_frame_arriving_after_teardown_does_not_resurrect_mirrors(
    tmp_path: Path,
) -> None:
    """The race behind CI run 43153344546's flaky teardown failure.

    Teardown clears the peer's mirrored agents, but the reader thread is still
    alive and can apply a registry frame the peer had already sent, putting
    them back. Those rows are owned by this process's pid, and `conjoin`
    outlives a dropped link, so a disconnected peer's agents would read as LIVE.
    Made deterministic here by sending the late frame after the run returns.
    """
    store = StateStore(root=tmp_path / "a")
    peer_r, peer_w = os.pipe()
    conn = Bridge(
        os.fdopen(peer_r, "rb"),
        io.BytesIO(),
        "hostA",
        store,
        poll_interval=0.05,
        sync_interval=10,
        recv_timeout=60,
    )
    runner = threading.Thread(target=conn.run, daemon=True)
    runner.start()
    agent = {
        "agent_id": "agent-bob",
        "name": "bob",
        "working_dir": "/b",
        "pid": os.getpid(),
        "pid_start": None,
        "last_seen": "2026-01-01T00:00:00Z",
    }
    registry = (json.dumps({"kind": "registry", "agents": [agent]}) + "\n").encode()
    with os.fdopen(peer_w, "wb") as peer:
        peer.write((json.dumps({"kind": "hello", "host": "hostB"}) + "\n").encode())
        peer.write(registry)
        peer.flush()
        _wait(lambda: any(a.agent_id == "agent-bob@hostB" for a in store.list_agents()))
        conn.stop()
        runner.join(timeout=5.0)
        assert not runner.is_alive()
        assert store.list_agents(include_stale=True) == []
        # The peer's in-flight frame lands after teardown; the reader is alive.
        peer.write(registry)
        peer.flush()
        time.sleep(0.3)
    assert [a.agent_id for a in store.list_agents(include_stale=True)] == []


def _late_peer_frames() -> bytes:
    agent = {
        "agent_id": "agent-bob",
        "name": "bob",
        "working_dir": "/b",
        "pid": os.getpid(),
        "pid_start": None,
        "last_seen": "2026-01-01T00:00:00Z",
    }
    return (
        json.dumps({"kind": "hello", "host": "hostB"})
        + "\n"
        + json.dumps({"kind": "registry", "agents": [agent]})
        + "\n"
    ).encode()


def test_a_hello_arriving_after_the_handshake_timeout_mirrors_nothing(
    tmp_path: Path,
) -> None:
    """Issue #83: #81's race, on the handshake-timeout exit.

    run() gives up on the hello and returns, but the reader thread is still
    alive. Before the fix it accepted a late hello, opened an attached peer
    record, and mirrored the registry that followed, so a peer that never
    connected read as LIVE until `conjoin`'s next attempt. Made deterministic
    by sending the late frames after run() returns.
    """
    store = StateStore(root=tmp_path / "a")
    peer_r, peer_w = os.pipe()
    conn = Bridge(os.fdopen(peer_r, "rb"), io.BytesIO(), "hostA", store, hello_timeout=0.2)
    with os.fdopen(peer_w, "wb") as peer:
        conn.run()  # returns on the timeout; the reader is still alive
        peer.write(_late_peer_frames())
        peer.flush()
        time.sleep(0.3)
    assert store.list_agents(include_stale=True) == []
    assert store.read_peer_link("hostB") is None


def test_a_hello_that_wins_the_race_with_the_handshake_timeout_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of #83's guard: a hello accepted before run() gave up.

    The wait can expire after the reader has already accepted the hello. That
    peer was accepted in time, so the link proceeds rather than leaving an
    accepted peer neither connected nor torn down. Forced deterministically by
    a wait that reports a timeout only once the hello has been handled.
    """

    class _LateEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            super().wait()
            return False

    store = StateStore(root=tmp_path / "a")
    peer_r, peer_w = os.pipe()
    conn = Bridge(
        os.fdopen(peer_r, "rb"),
        io.BytesIO(),
        "hostA",
        store,
        poll_interval=0.05,
        sync_interval=10,
        recv_timeout=60,
    )
    monkeypatch.setattr(conn, "_hello", _LateEvent())
    runner = threading.Thread(target=conn.run, daemon=True)
    runner.start()
    with os.fdopen(peer_w, "wb") as peer:
        peer.write(_late_peer_frames())
        peer.flush()
        _wait(lambda: any(a.agent_id == "agent-bob@hostB" for a in store.list_agents()))
        conn.stop()
        runner.join(timeout=5.0)
    assert not runner.is_alive()
    assert store.list_agents(include_stale=True) == []  # torn down normally
    link = store.read_peer_link("hostB")
    assert link is not None and link.detached_at is not None
