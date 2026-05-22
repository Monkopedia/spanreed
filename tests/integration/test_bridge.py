"""Integration test for the cross-host bus-bridge.

Wires two ``_Bridge`` instances together over in-process pipes (standing in
for the SSH duplex pipe), each backed by its own StateStore, and checks that
agents mirror across and messages flow in both directions.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock

from spanreed.bridge import Bridge, reconnect_loop
from spanreed.store import StateStore


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
