"""Tests for sleeper records and their place on the bus (stage 1: no waking yet).

``docs/architecture.md``, "Sleepers": the record decides a sleeper's presence,
not its pid, at every site that used to ask ``is_stale``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spanreed import store as store_module
from spanreed.protocol import Agent
from spanreed.sleepers import SleeperError, SleeperRecord, parse_duration
from spanreed.store import AgentIdHeld, StateStore, runner_is_alive


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _record(name: str = "nap", **overrides: object) -> SleeperRecord:
    fields: dict[str, object] = {
        "name": name,
        "agent_id": f"agent-{name}",
        "cwd": "/tmp",
        "session_id": "00000000-0000-4000-8000-000000000000",
        "permission_mode": "acceptEdits",
        "claude_path": "/usr/bin/claude",
        "spanreed_path": "/usr/bin/spanreed",
        "path_env": "/usr/bin",
        "created_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return SleeperRecord.model_validate(fields)


def _set_state(store: StateStore, name: str, **update: object) -> None:
    """Move a record to a state stage 1 cannot reach by itself (no runner yet)."""
    with store.sleepers.lock(name):
        record = store.sleepers.read(name)
        assert record is not None
        store.sleepers.write(record.model_copy(update=update))


class TestParseDuration:
    @pytest.mark.parametrize(
        ("text", "seconds"), [("900", 900), ("30s", 30), ("15m", 900), ("2h", 7200)]
    )
    def test_accepted_forms(self, text: str, seconds: int) -> None:
        assert parse_duration(text) == seconds

    @pytest.mark.parametrize("text", ["", "0", "-5", "15 minutes", "1.5h", "m"])
    def test_rejected_forms(self, text: str) -> None:
        with pytest.raises(ValueError, match="not a duration"):
            parse_duration(text)


class TestCreate:
    def test_starts_asleep_with_a_processless_row(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        record = store.sleepers.read("nap")
        assert record is not None
        assert record.state == "asleep"
        [row] = store.list_agents()
        assert (row.agent_id, row.pid, row.sleeper) == ("agent-nap", 0, "asleep")
        assert row.pid_start == -1

    def test_record_is_owner_only(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        mode = stat.S_IMODE((store.root / "sleepers" / "nap.json").stat().st_mode)
        assert mode == 0o600
        assert stat.S_IMODE((store.root / "sleepers").stat().st_mode) == 0o700

    def test_read_through_starts_at_the_end_of_an_existing_inbox(self, store: StateStore) -> None:
        """retire keeps the inbox; a re-created sleeper must not replay its history."""
        store.create_sleeper(_record())
        first = store.send_message("tester", "agent-nap", "one")
        second = store.send_message("tester", "agent-nap", "two")
        store.retire_sleeper("nap")
        record = store.create_sleeper(_record())
        assert (record.read_through.line, record.read_through.msg_id) == (2, second.msg_id)
        assert first.msg_id != second.msg_id

    def test_read_through_is_zero_for_a_new_inbox(self, store: StateStore) -> None:
        record = store.create_sleeper(_record())
        assert (record.read_through.line, record.read_through.msg_id) == (0, None)

    def test_an_existing_sleeper_is_refused(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        with pytest.raises(SleeperError, match="already exists"):
            store.create_sleeper(_record(cwd="/elsewhere"))
        record = store.sleepers.read("nap")
        assert record is not None and record.cwd == "/tmp"

    def test_a_live_holder_of_the_id_is_refused(self, store: StateStore) -> None:
        store.register_agent(
            name="someone", working_dir="/x", pid=os.getpid(), agent_id="agent-nap"
        )
        with pytest.raises(SleeperError, match="held by a running agent: agent-nap"):
            store.create_sleeper(_record())
        assert store.sleepers.read("nap") is None

    def test_a_live_holder_of_the_display_name_is_refused(self, store: StateStore) -> None:
        """Mail sent to the name would be ambiguous between the two."""
        store.register_agent(name="nap", working_dir="/x", pid=os.getpid(), agent_id="agent-zz")
        with pytest.raises(SleeperError, match="agent-zz"):
            store.create_sleeper(_record())

    def test_a_holder_that_registers_during_create_is_not_replaced(
        self, store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Review of #89: create checks first and writes second; a Codex worker
        registering in between must keep its row, and nothing is created."""
        real_end = store._inbox_end  # pyright: ignore[reportPrivateUsage]

        def register_meanwhile(agent_id: str) -> object:
            store.register_agent(name="w", working_dir="/", pid=os.getpid(), agent_id="agent-nap")
            return real_end(agent_id)

        monkeypatch.setattr(store, "_inbox_end", register_meanwhile)
        with pytest.raises(SleeperError, match="was taken by agent-nap"):
            store.create_sleeper(_record())
        assert store.sleepers.read("nap") is None
        [row] = store.list_agents()
        assert (row.name, row.pid, row.sleeper) == ("w", os.getpid(), None)

    def test_a_stale_row_on_the_id_is_replaced(self, store: StateStore) -> None:
        store.register_agent(name="old", working_dir="/x", pid=_dead_pid(), agent_id="agent-nap")
        store.create_sleeper(_record())
        rows = [a for a in store.list_agents(include_stale=True) if a.agent_id == "agent-nap"]
        assert [(a.name, a.pid, a.sleeper) for a in rows] == [("nap", 0, "asleep")]


class TestPresence:
    def test_pid_zero_is_never_alive(self) -> None:
        """kill(0, 0) succeeds (it signals our own group), so pid 0 must be special."""
        assert not store_module._is_pid_alive(0)  # pyright: ignore[reportPrivateUsage]
        row = Agent(agent_id="x", name="x", working_dir="/", pid=0, last_seen=datetime.now(UTC))
        assert store_module.is_stale(row)

    def test_an_older_binary_reads_the_asleep_row_as_stale(self, store: StateStore) -> None:
        """Review of #89: main's liveness test, verbatim, on the row as written to disk.

        kill(0, 0) succeeds, so with pid_start null an older spanreed would list an
        asleep sleeper as LIVE and tell senders "Delivered". The row's pid_start
        can never match, so it reads as stale: true for a binary that cannot wake it.
        """
        store.create_sleeper(_record())

        def old_is_stale(agent: Agent) -> bool:  # src/spanreed/store.py at 0a4a1e5
            try:
                os.kill(agent.pid, 0)
            except ProcessLookupError:
                return True
            except PermissionError:
                pass
            if agent.pid_start is None:
                return False
            return store_module.pid_start_time(agent.pid) != agent.pid_start

        raw = json.loads((store.root / "registry.json").read_text())["agents"]
        [row] = [Agent.model_validate(a) for a in raw]
        assert (row.pid, row.pid_start) == (0, -1)
        assert old_is_stale(row)
        assert store.list_agents() != [], "new code still lists it, from the record"

    def test_an_asleep_sleeper_is_listed_though_it_has_no_process(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        assert [a.agent_id for a in store.list_agents()] == ["agent-nap"]

    def test_a_missing_row_is_rebuilt_from_the_record(self, store: StateStore) -> None:
        """An older binary's prune drops the row; the record keeps it addressable."""
        store.create_sleeper(_record())
        store.deregister_agent("agent-nap")
        [row] = store.list_agents()
        assert (row.agent_id, row.sleeper) == ("agent-nap", "asleep")
        assert store.send_message("tester", "nap", "still reachable").to_agent == "agent-nap"

    def test_the_row_shows_the_records_state_not_its_own(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        _set_state(store, "nap", state="awake", runner_pid=os.getpid())
        [row] = store.list_agents()
        assert row.sleeper == "awake"

    def test_prune_never_removes_a_sleeper(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        store.register_agent(name="dead", working_dir="/", pid=_dead_pid(), agent_id="dead")
        assert store.prune_stale() == 1
        ids = [a.agent_id for a in store.list_agents(include_stale=True)]
        assert ids == ["agent-nap"]

    def test_an_asleep_sleeper_resolves_by_display_name(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        msg = store.send_message("tester", "nap", "hello")
        assert msg.to_agent == "agent-nap"
        assert [m.msg_id for m in store.recv_messages("agent-nap")] == [msg.msg_id]

    def test_a_sleeper_holds_its_id_against_refuse_live_holder(self, store: StateStore) -> None:
        """A Codex worker registers with refuse_live_holder; an asleep sleeper has no pid."""
        store.create_sleeper(_record())
        with pytest.raises(AgentIdHeld, match="held by sleeper nap"):
            store.register_agent(
                name="nap",
                working_dir="/",
                pid=os.getpid(),
                agent_id="agent-nap",
                refuse_live_holder=True,
            )

    def test_a_plain_upsert_keeps_the_sleeper(self, store: StateStore) -> None:
        """The woken Claude's SessionStart re-registers the id with its own pid."""
        store.create_sleeper(_record())
        store.register_agent(name="nap", working_dir="/tmp", pid=os.getpid(), agent_id="agent-nap")
        [row] = store.list_agents()
        assert (row.pid, row.sleeper) == (os.getpid(), "asleep")

    def test_a_mirrored_sleeper_is_present_only_while_its_bridge_is(
        self, store: StateStore
    ) -> None:
        remote = Agent(
            agent_id="agent-nap",
            name="nap",
            working_dir="/",
            pid=0,
            last_seen=datetime.now(UTC),
            sleeper="asleep",
        )
        store.sync_remote_agents("hostB", [remote], owner_pid=_dead_pid(), owner_pid_start=None)
        [row] = store.list_agents(include_stale=True)
        assert (row.agent_id, row.sleeper) == ("agent-nap@hostB", "asleep")
        assert store.list_agents() == []


class TestDeliveryVerdict:
    def test_asleep_is_queued_and_carries_the_wake_failure(self, store: StateStore) -> None:
        """Mail wakes a sleeper, so asleep after a send means the wake failed."""
        store.create_sleeper(_record())
        store.send_message("tester", "agent-nap", "hello")  # the test fixture refuses tmux
        delivered, why = store.delivery_verdict("agent-nap")
        assert not delivered
        assert "ASLEEP and did not wake" in why
        assert "tests do not launch tmux" in why

    def test_awake_with_a_live_runner_is_delivered(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        _set_state(
            store,
            "nap",
            state="awake",
            runner_pid=os.getpid(),
            runner_pid_start=store_module.pid_start_time(os.getpid()),
            tmux_session="spanreed-nap-1",
        )
        delivered, why = store.delivery_verdict("agent-nap")
        assert delivered
        assert "AWAKE" in why and "spanreed-nap-1" in why

    @pytest.mark.parametrize("state", ["awake", "sleeping"])
    def test_a_dead_runner_is_queued_and_named(self, store: StateStore, state: str) -> None:
        store.create_sleeper(_record())
        dead = _dead_pid()
        _set_state(store, "nap", state=state, runner_pid=dead)
        delivered, why = store.delivery_verdict("agent-nap")
        assert not delivered
        assert f"runner (pid {dead}) is gone" in why

    def test_sleeping_with_a_live_runner_is_queued_for_the_check(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        _set_state(store, "nap", state="sleeping", runner_pid=os.getpid())
        delivered, why = store.delivery_verdict("agent-nap")
        assert not delivered
        assert "GOING TO SLEEP" in why


class TestRunnerIsAlive:
    def test_no_runner(self) -> None:
        assert not runner_is_alive(_record())

    def test_a_recycled_pid_is_dead(self) -> None:
        start = store_module.pid_start_time(os.getpid())
        if start is None:
            pytest.skip("no /proc start times on this platform")
        record = _record(runner_pid=os.getpid(), runner_pid_start=start + 1)
        assert not runner_is_alive(record)

    def test_a_matching_pid_is_alive(self) -> None:
        start = store_module.pid_start_time(os.getpid())
        assert runner_is_alive(_record(runner_pid=os.getpid(), runner_pid_start=start))


class TestRetire:
    def test_removes_record_and_row_but_keeps_the_inbox(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        store.send_message("tester", "agent-nap", "history")
        store.retire_sleeper("nap")
        assert store.sleepers.read("nap") is None
        assert store.list_agents(include_stale=True) == []
        assert [m.body for m in store.recv_messages("agent-nap")] == ["history"]

    def test_mail_to_a_retired_sleeper_is_refused(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        store.retire_sleeper("nap")
        with pytest.raises(ValueError, match="not a registered agent_id"):
            store.send_message("tester", "nap", "too late")

    def test_an_unknown_name_is_refused(self, store: StateStore) -> None:
        with pytest.raises(SleeperError, match="no sleeper named"):
            store.retire_sleeper("ghost")

    def test_an_awake_sleeper_is_refused(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        _set_state(store, "nap", state="awake", runner_pid=os.getpid())
        with pytest.raises(SleeperError, match="must be asleep to be retired"):
            store.retire_sleeper("nap")
        assert store.sleepers.read("nap") is not None

    def test_a_running_claude_without_a_runner_is_refused(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        _set_state(store, "nap", state="awake", runner_pid=_dead_pid(), claude_pid=os.getpid())
        with pytest.raises(SleeperError, match="must be asleep"):
            store.retire_sleeper("nap")

    def test_a_crashed_awake_sleeper_can_be_retired(self, store: StateStore) -> None:
        """Its record says awake, but nothing of it is running."""
        store.create_sleeper(_record())
        _set_state(store, "nap", state="awake", runner_pid=_dead_pid(), claude_pid=_dead_pid())
        store.retire_sleeper("nap")
        assert store.sleepers.read("nap") is None


def test_records_survive_a_new_store_instance(state_root: Path) -> None:
    """The record is on disk, so it outlives the process (and a reboot)."""
    StateStore(root=state_root).create_sleeper(_record())
    [row] = StateStore(root=state_root).list_agents()
    assert row.sleeper == "asleep"
