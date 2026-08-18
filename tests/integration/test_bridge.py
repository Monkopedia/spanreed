"""Integration test for the cross-host bus-bridge.

Wires two ``_Bridge`` instances together over in-process pipes (standing in
for the SSH duplex pipe), each backed by its own StateStore, and checks that
agents mirror across and messages flow in both directions.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
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
        """The refusal guard must key on ``peer_host``, not on ``_stop``.

        ``_stop`` is set by a plain EOF too, and an EOF can land *after* a valid
        hello — a peer that says hello, sends its registry, then closes. That
        path has mirrored entries to clean up. Keyed on ``_stop``, the early
        return skips ``clear_remote_agents`` and leaves them behind.

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
