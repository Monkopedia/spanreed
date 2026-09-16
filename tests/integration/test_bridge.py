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
from typing import cast
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
