"""Tests for the filesystem-backed StateStore."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spanreed import store as store_module
from spanreed.protocol import Agent, Message, PeerLink
from spanreed.store import (
    StateStore,
    format_age,
    peer_link_is_attached,
)


def _dead_pid() -> int:
    """Spawn-and-reap a subprocess to get a PID guaranteed not in use."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@pytest.fixture
def ab_registered(store: StateStore) -> StateStore:
    """Register toy recipients ``A`` and ``B`` in the same store.

    ``send_message`` now rejects unregistered recipients, so the inbox/cursor/
    wait tests (which post to bare ids) need those ids on the registry first.
    Depends on ``store`` so a test requesting both gets the one instance.
    """
    for aid in ("A", "B"):
        store.register_agent(name=aid, working_dir="/tmp", pid=os.getpid(), agent_id=aid)
    return store


# ----------------------------------------------------------------- registry


class TestRegistry:
    def test_register_returns_agent_with_id(self, store: StateStore) -> None:
        agent = store.register_agent(name="alice", working_dir="/tmp/a", pid=os.getpid())
        assert agent.agent_id.startswith("agent-")
        assert agent.name == "alice"
        assert agent.working_dir == "/tmp/a"
        assert agent.pid == os.getpid()

    def test_register_appears_in_list(self, store: StateStore) -> None:
        agent = store.register_agent(name="alice", working_dir="/tmp/a", pid=os.getpid())
        assert any(a.agent_id == agent.agent_id for a in store.list_agents())

    def test_deregister_removes_from_list(self, store: StateStore) -> None:
        agent = store.register_agent(name="alice", working_dir="/tmp/a", pid=os.getpid())
        store.deregister_agent(agent.agent_id)
        assert not any(a.agent_id == agent.agent_id for a in store.list_agents())

    def test_deregister_unknown_is_noop(self, store: StateStore) -> None:
        # Just shouldn't raise.
        store.deregister_agent("agent-does-not-exist")

    def test_list_filters_stale_pid(self, store: StateStore) -> None:
        alive = store.register_agent(name="live", working_dir="/x", pid=os.getpid())
        dead = store.register_agent(name="dead", working_dir="/y", pid=_dead_pid())
        ids = {a.agent_id for a in store.list_agents()}
        assert alive.agent_id in ids
        assert dead.agent_id not in ids

    def test_list_include_stale_returns_dead(self, store: StateStore) -> None:
        dead = store.register_agent(name="dead", working_dir="/y", pid=_dead_pid())
        ids = {a.agent_id for a in store.list_agents(include_stale=True)}
        assert dead.agent_id in ids

    def test_prune_stale_mutates_registry(self, store: StateStore) -> None:
        store.register_agent(name="dead-1", working_dir="/y", pid=_dead_pid())
        store.register_agent(name="dead-2", working_dir="/y", pid=_dead_pid())
        store.register_agent(name="live", working_dir="/x", pid=os.getpid())
        removed = store.prune_stale()
        assert removed == 2
        assert len(store.list_agents(include_stale=True)) == 1

    def test_register_captures_pid_start(self, store: StateStore) -> None:
        agent = store.register_agent(name="alice", working_dir="/x", pid=os.getpid())
        assert agent.pid_start == store_module.pid_start_time(os.getpid())

    def test_register_with_explicit_id(self, store: StateStore) -> None:
        agent = store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-fixed"
        )
        assert agent.agent_id == "agent-fixed"

    def test_register_upsert_refreshes_only_pid_and_last_seen(self, store: StateStore) -> None:
        """Re-registering an existing agent_id preserves name/working_dir/focus
        and only refreshes pid + last_seen. This keeps in-session customizations
        (manual rename, focus) sticky across session restarts."""
        first = store.register_agent(
            name="alice-old", working_dir="/x", pid=12345, agent_id="agent-fixed"
        )
        time.sleep(0.01)
        second = store.register_agent(
            name="alice-new",
            working_dir="/y",
            pid=67890,
            agent_id="agent-fixed",
        )
        assert second.agent_id == first.agent_id
        matching = [a for a in store.list_agents(include_stale=True) if a.agent_id == "agent-fixed"]
        assert len(matching) == 1
        # Preserved.
        assert matching[0].name == "alice-old"
        assert matching[0].working_dir == "/x"
        # Refreshed.
        assert matching[0].pid == 67890
        assert matching[0].last_seen > first.last_seen


# ----------------------------------------------------------------- focus


class TestFocus:
    def test_set_focus_returns_updated_agent(self, store: StateStore) -> None:
        agent = store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-alice"
        )
        updated = store.set_focus(agent.agent_id, "working on auth refactor")
        assert updated is not None
        assert updated.focus == "working on auth refactor"

    def test_set_focus_persists_in_list(self, store: StateStore) -> None:
        store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-alice"
        )
        store.set_focus("agent-alice", "the focus")
        listed = next(a for a in store.list_agents() if a.agent_id == "agent-alice")
        assert listed.focus == "the focus"

    def test_set_focus_on_unknown_returns_none(self, store: StateStore) -> None:
        assert store.set_focus("agent-does-not-exist", "something") is None

    def test_set_focus_empty_string_clears(self, store: StateStore) -> None:
        store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-alice"
        )
        store.set_focus("agent-alice", "the focus")
        cleared = store.set_focus("agent-alice", "")
        assert cleared is not None
        assert cleared.focus is None

    def test_set_focus_none_clears(self, store: StateStore) -> None:
        store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-alice"
        )
        store.set_focus("agent-alice", "the focus")
        cleared = store.set_focus("agent-alice", None)
        assert cleared is not None
        assert cleared.focus is None

    def test_focus_preserved_across_reregister(self, store: StateStore) -> None:
        store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-alice"
        )
        store.set_focus("agent-alice", "the focus")
        # Re-register the same agent_id (as the SessionStart hook would on restart).
        store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-alice"
        )
        after = next(a for a in store.list_agents() if a.agent_id == "agent-alice")
        assert after.focus == "the focus"

    def test_register_does_not_set_focus_by_default(self, store: StateStore) -> None:
        agent = store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-alice"
        )
        assert agent.focus is None


# ----------------------------------------------------------------- status


class TestStatus:
    def test_set_status_returns_updated_agent(self, store: StateStore) -> None:
        store.register_agent(name="a", working_dir="/x", pid=os.getpid(), agent_id="agent-a")
        updated = store.set_status("agent-a", "blocked")
        assert updated is not None
        assert updated.status == "blocked"

    def test_set_status_persists_in_list(self, store: StateStore) -> None:
        store.register_agent(name="a", working_dir="/x", pid=os.getpid(), agent_id="agent-a")
        store.set_status("agent-a", "needs_input")
        listed = next(a for a in store.list_agents() if a.agent_id == "agent-a")
        assert listed.status == "needs_input"

    def test_set_status_on_unknown_returns_none(self, store: StateStore) -> None:
        assert store.set_status("agent-nope", "working") is None

    def test_register_does_not_set_status_by_default(self, store: StateStore) -> None:
        agent = store.register_agent(
            name="a", working_dir="/x", pid=os.getpid(), agent_id="agent-a"
        )
        assert agent.status is None

    def test_status_reset_on_reregister(self, store: StateStore) -> None:
        """Unlike focus, status is NOT preserved across re-registration — a stale
        status from a prior session would mislead the fleet view."""
        store.register_agent(name="a", working_dir="/x", pid=os.getpid(), agent_id="agent-a")
        store.set_status("agent-a", "blocked")
        store.set_focus("agent-a", "the focus")
        # Re-register, as the SessionStart hook does on restart.
        store.register_agent(name="a", working_dir="/x", pid=os.getpid(), agent_id="agent-a")
        after = next(a for a in store.list_agents() if a.agent_id == "agent-a")
        assert after.status is None  # reset
        assert after.focus == "the focus"  # preserved


# ----------------------------------------------------------------- status tracking flag


class TestStatusTracking:
    def test_default_is_off(self, store: StateStore) -> None:
        assert store.get_status_tracking() is False

    def test_enable_then_read(self, store: StateStore) -> None:
        store.set_status_tracking(True)
        assert store.get_status_tracking() is True

    def test_disable_then_read(self, store: StateStore) -> None:
        store.set_status_tracking(True)
        store.set_status_tracking(False)
        assert store.get_status_tracking() is False

    def test_flag_persists_across_store_instances(self, state_root: Path) -> None:
        StateStore(root=state_root).set_status_tracking(True)
        assert StateStore(root=state_root).get_status_tracking() is True

    def test_malformed_config_reads_as_off(self, store: StateStore) -> None:
        (store.root / "config.json").write_text("not json{")
        assert store.get_status_tracking() is False


# ----------------------------------------------------------------- name


class TestSetName:
    def test_set_name_returns_updated_agent(self, store: StateStore) -> None:
        store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-alice"
        )
        updated = store.set_name("agent-alice", "main-coordinator")
        assert updated is not None
        assert updated.name == "main-coordinator"

    def test_set_name_persists_in_list(self, store: StateStore) -> None:
        store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-alice"
        )
        store.set_name("agent-alice", "renamed")
        listed = next(a for a in store.list_agents() if a.agent_id == "agent-alice")
        assert listed.name == "renamed"

    def test_set_name_on_unknown_returns_none(self, store: StateStore) -> None:
        assert store.set_name("agent-does-not-exist", "whatever") is None

    def test_name_preserved_across_reregister(self, store: StateStore) -> None:
        store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-alice"
        )
        store.set_name("agent-alice", "renamed")
        # Simulate session restart: hook re-registers with derived name.
        store.register_agent(
            name="alice", working_dir="/x", pid=os.getpid(), agent_id="agent-alice"
        )
        after = next(a for a in store.list_agents() if a.agent_id == "agent-alice")
        assert after.name == "renamed"

    def test_registry_persists_across_store_instances(self, state_root: Path) -> None:
        s1 = StateStore(root=state_root)
        agent = s1.register_agent(name="alice", working_dir="/x", pid=os.getpid())
        s2 = StateStore(root=state_root)
        assert any(a.agent_id == agent.agent_id for a in s2.list_agents())


# ----------------------------------------------------------------- staleness via PID identity


class TestStaleness:
    def test_stale_if_pid_dead(self) -> None:
        agent = Agent(
            agent_id="x",
            name="x",
            working_dir="/x",
            pid=_dead_pid(),
            pid_start=store_module.pid_start_time(os.getpid()),
            last_seen=datetime.now(UTC),
        )
        assert store_module.is_stale(agent)

    def test_stale_if_pid_reused(self) -> None:
        """PID is alive, but the recorded start-time doesn't match — i.e. the
        original agent died and an unrelated process recycled its PID."""
        agent = Agent(
            agent_id="x",
            name="x",
            working_dir="/x",
            pid=os.getpid(),
            pid_start=1,  # bogus: real start-time is clock-ticks-since-boot, never 1
            last_seen=datetime.now(UTC),
        )
        assert store_module.is_stale(agent)

    def test_fresh_with_live_pid_matching_start(self) -> None:
        agent = Agent(
            agent_id="x",
            name="x",
            working_dir="/x",
            pid=os.getpid(),
            pid_start=store_module.pid_start_time(os.getpid()),
            last_seen=datetime.now(UTC),
        )
        assert not store_module.is_stale(agent)

    def test_live_pid_without_start_time_is_not_stale(self) -> None:
        """Degradation path (no /proc, e.g. macOS): trust the bare PID check
        rather than flagging every agent stale."""
        agent = Agent(
            agent_id="x",
            name="x",
            working_dir="/x",
            pid=os.getpid(),
            pid_start=None,
            last_seen=datetime.now(UTC),
        )
        assert not store_module.is_stale(agent)

    def test_stale_when_no_start_time_and_pid_dead(self) -> None:
        agent = Agent(
            agent_id="x",
            name="x",
            working_dir="/x",
            pid=_dead_pid(),
            pid_start=None,
            last_seen=datetime.now(UTC),
        )
        assert store_module.is_stale(agent)


# ------------------------------------------------------- cross-host bridge primitives


class TestBridgePrimitives:
    def _msg(
        self, msg_id: str, to_agent: str = "agent-bob", in_reply_to: str | None = None
    ) -> Message:
        return Message(
            msg_id=msg_id,
            from_agent="agent-alice@hostA",
            to_agent=to_agent,
            body="hi",
            ts=datetime.now(UTC),
            in_reply_to=in_reply_to,
        )

    def test_append_message_preserves_fields(self, store: StateStore) -> None:
        store.append_message(self._msg("msg-fixed", in_reply_to="msg-prev"))
        got = store.recv_messages("agent-bob")
        assert len(got) == 1
        assert got[0].msg_id == "msg-fixed"
        assert got[0].from_agent == "agent-alice@hostA"
        assert got[0].in_reply_to == "msg-prev"

    def test_append_message_dedupes_by_id(self, store: StateStore) -> None:
        store.append_message(self._msg("msg-dup"))
        store.append_message(self._msg("msg-dup"))
        assert len(store.recv_messages("agent-bob")) == 1

    def test_sync_remote_agents_mirrors_with_owner_pid(self, store: StateStore) -> None:
        remote = Agent(
            agent_id="agent-x",
            name="x",
            working_dir="/r",
            pid=999_999,  # the remote's pid, meaningless locally
            last_seen=datetime.now(UTC),
            focus="doing things",
            status="blocked",
        )
        owner_start = store_module.pid_start_time(os.getpid())
        store.sync_remote_agents(
            "hostB", [remote], owner_pid=os.getpid(), owner_pid_start=owner_start
        )
        listed = {a.agent_id: a for a in store.list_agents()}
        assert "agent-x@hostB" in listed
        # Mirrored entry rides the bridge's pid, so it's live while the bridge is.
        assert listed["agent-x@hostB"].pid == os.getpid()
        assert listed["agent-x@hostB"].focus == "doing things"
        # status crosses the bridge alongside focus, so "who needs a human" sees
        # remote agents too.
        assert listed["agent-x@hostB"].status == "blocked"

    def test_sync_remote_agents_replaces_previous_snapshot(self, store: StateStore) -> None:
        a = Agent(
            agent_id="agent-a",
            name="a",
            working_dir="/r",
            pid=os.getpid(),
            last_seen=datetime.now(UTC),
        )
        b = Agent(
            agent_id="agent-b",
            name="b",
            working_dir="/r",
            pid=os.getpid(),
            last_seen=datetime.now(UTC),
        )
        start = store_module.pid_start_time(os.getpid())
        store.sync_remote_agents("hostB", [a], os.getpid(), start)
        store.sync_remote_agents("hostB", [b], os.getpid(), start)
        ids = {ag.agent_id for ag in store.list_agents()}
        assert "agent-b@hostB" in ids
        assert "agent-a@hostB" not in ids  # departed remote agent dropped

    def test_clear_remote_agents_removes_only_that_host(self, store: StateStore) -> None:
        start = store_module.pid_start_time(os.getpid())
        b = Agent(
            agent_id="agent-b",
            name="b",
            working_dir="/r",
            pid=os.getpid(),
            last_seen=datetime.now(UTC),
        )
        c = Agent(
            agent_id="agent-c",
            name="c",
            working_dir="/r",
            pid=os.getpid(),
            last_seen=datetime.now(UTC),
        )
        store.sync_remote_agents("hostB", [b], os.getpid(), start)
        store.sync_remote_agents("hostC", [c], os.getpid(), start)
        store.clear_remote_agents("hostB")
        ids = {ag.agent_id for ag in store.list_agents()}
        assert "agent-b@hostB" not in ids
        assert "agent-c@hostC" in ids


# ----------------------------------------------------------------- inboxes


class TestInboxes:
    def test_send_creates_inbox_if_missing(
        self, store: StateStore, ab_registered: StateStore
    ) -> None:
        msg = store.send_message(from_agent="A", to_agent="B", body="hello")
        assert msg.msg_id.startswith("msg-")
        assert msg.body == "hello"
        assert store.recv_messages("B") == [msg]

    def test_recv_returns_empty_for_unknown_agent(self, store: StateStore) -> None:
        assert store.recv_messages("nobody") == []

    def test_send_then_recv_round_trip(self, store: StateStore, ab_registered: StateStore) -> None:
        m1 = store.send_message(from_agent="A", to_agent="B", body="one")
        m2 = store.send_message(from_agent="A", to_agent="B", body="two")
        assert store.recv_messages("B") == [m1, m2]

    def test_in_reply_to_round_trip(self, store: StateStore, ab_registered: StateStore) -> None:
        m1 = store.send_message(from_agent="A", to_agent="B", body="ping")
        m2 = store.send_message(from_agent="B", to_agent="A", body="pong", in_reply_to=m1.msg_id)
        delivered = store.recv_messages("A")
        assert delivered == [m2]
        assert delivered[0].in_reply_to == m1.msg_id


# ----------------------------------------------------------------- cursors


class TestCursors:
    def test_recv_with_cursor_returns_only_new(
        self, store: StateStore, ab_registered: StateStore
    ) -> None:
        m1 = store.send_message(from_agent="A", to_agent="B", body="one")
        m2 = store.send_message(from_agent="A", to_agent="B", body="two")
        m3 = store.send_message(from_agent="A", to_agent="B", body="three")
        assert store.recv_messages("B", since_msg_id=m1.msg_id) == [m2, m3]

    def test_recv_with_unknown_cursor_returns_all(
        self, store: StateStore, ab_registered: StateStore
    ) -> None:
        m1 = store.send_message(from_agent="A", to_agent="B", body="one")
        assert store.recv_messages("B", since_msg_id="msg-deadbeef") == [m1]

    def test_cursor_get_returns_none_when_unset(self, store: StateStore) -> None:
        assert store.get_cursor("session-1") is None

    def test_cursor_set_then_get(self, store: StateStore) -> None:
        store.set_cursor("session-1", "msg-abc")
        assert store.get_cursor("session-1") == "msg-abc"

    def test_cursor_set_overwrites(self, store: StateStore) -> None:
        store.set_cursor("session-1", "msg-abc")
        store.set_cursor("session-1", "msg-def")
        assert store.get_cursor("session-1") == "msg-def"


# ----------------------------------------------------------------- wait_for_reply


class TestWaitForReply:
    def test_returns_matching_reply(self, store: StateStore, ab_registered: StateStore) -> None:
        m1 = store.send_message(from_agent="A", to_agent="B", body="ping")

        result: list[object] = []

        def waiter() -> None:
            result.append(store.wait_for_reply(agent_id="A", in_reply_to=m1.msg_id, timeout_s=2.0))

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.2)
        reply = store.send_message(from_agent="B", to_agent="A", body="pong", in_reply_to=m1.msg_id)
        t.join(timeout=2.0)
        assert result == [reply]

    def test_times_out_when_no_reply(self, store: StateStore, ab_registered: StateStore) -> None:
        m1 = store.send_message(from_agent="A", to_agent="B", body="ping")
        result = store.wait_for_reply(
            agent_id="A",
            in_reply_to=m1.msg_id,
            timeout_s=0.2,
            poll_interval_s=0.05,
        )
        assert result is None

    def test_returns_existing_matching_message_immediately(
        self, store: StateStore, ab_registered: StateStore
    ) -> None:
        """wait_for_reply should find a matching reply already in the inbox.

        This is the fix for the race where a reply lands between the caller's
        send_message and wait_for_reply calls.
        """
        m1 = store.send_message(from_agent="A", to_agent="B", body="ping")
        reply = store.send_message(
            from_agent="B", to_agent="A", body="already-there", in_reply_to=m1.msg_id
        )
        result = store.wait_for_reply(
            agent_id="A",
            in_reply_to=m1.msg_id,
            timeout_s=0.2,
            poll_interval_s=0.05,
        )
        assert result == reply

    def test_ignores_unrelated_messages(self, store: StateStore, ab_registered: StateStore) -> None:
        m1 = store.send_message(from_agent="A", to_agent="B", body="ping")
        result_holder: list[object] = []

        def waiter() -> None:
            result_holder.append(
                store.wait_for_reply(
                    agent_id="A",
                    in_reply_to=m1.msg_id,
                    timeout_s=0.5,
                    poll_interval_s=0.05,
                )
            )

        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.1)
        # Unrelated message — should NOT satisfy the wait.
        store.send_message(from_agent="C", to_agent="A", body="unrelated")
        t.join(timeout=1.0)
        assert result_holder == [None]


# ----------------------------------------------------------- recipient validation


class TestRecipientValidation:
    """send_message must not silently black-hole a message to a dead inbox."""

    def test_unknown_recipient_raises(self, store: StateStore) -> None:
        with pytest.raises(ValueError, match="not a registered"):
            store.send_message(from_agent="A", to_agent="ghost", body="x")

    def test_known_id_delivers(self, store: StateStore) -> None:
        store.register_agent(name="bee", working_dir="/tmp", pid=os.getpid(), agent_id="agent-bee")
        msg = store.send_message(from_agent="A", to_agent="agent-bee", body="x")
        assert store.recv_messages("agent-bee") == [msg]

    def test_display_name_resolves_to_id(self, store: StateStore) -> None:
        # The real-world bug: addressing by name instead of agent_id. Now it
        # resolves to the id and lands in the inbox the recipient actually tails.
        store.register_agent(
            name="ksrpc", working_dir="/tmp", pid=os.getpid(), agent_id="agent-345940f7"
        )
        msg = store.send_message(from_agent="A", to_agent="ksrpc", body="hi")
        assert msg.to_agent == "agent-345940f7"
        assert store.recv_messages("agent-345940f7") == [msg]
        assert store.recv_messages("ksrpc") == []  # nothing left in a dead name-inbox

    def test_ambiguous_display_name_raises(self, store: StateStore) -> None:
        store.register_agent(name="dup", working_dir="/tmp", pid=os.getpid(), agent_id="agent-1")
        store.register_agent(name="dup", working_dir="/tmp", pid=os.getpid(), agent_id="agent-2")
        with pytest.raises(ValueError, match="display name shared by multiple live agents"):
            store.send_message(from_agent="A", to_agent="dup", body="x")

    def test_stale_recipient_still_delivers(self, store: StateStore) -> None:
        # A crashed-but-not-pruned agent stays addressable; mail waits for its
        # restart rather than erroring (resolution uses the include-stale view).
        store.register_agent(name="z", working_dir="/tmp", pid=_dead_pid(), agent_id="agent-z")
        msg = store.send_message(from_agent="A", to_agent="agent-z", body="x")
        assert store.recv_messages("agent-z") == [msg]


# ----------------------------------------------------------------- activity log


class TestActivityLog:
    def _reg(self, store: StateStore, agent_id: str, name: str) -> None:
        store.register_agent(name=name, working_dir="/x", pid=os.getpid(), agent_id=agent_id)

    def test_flag_defaults_off_and_roundtrips(self, store: StateStore) -> None:
        assert store.get_activity_log() is False
        store.set_activity_log(True)
        assert store.get_activity_log() is True
        store.set_activity_log(False)
        assert store.get_activity_log() is False

    def test_disabled_logs_nothing(self, store: StateStore) -> None:
        self._reg(store, "agent-a", "a")
        store.set_focus("agent-a", "doing things")
        store.set_status("agent-a", "working")
        assert store.read_activity() == []

    def test_enabled_logs_focus_and_status_changes(self, store: StateStore) -> None:
        self._reg(store, "agent-a", "kodemirror")
        store.set_activity_log(True)
        store.set_focus("agent-a", "vim-scroll cluster")
        store.set_status("agent-a", "blocked")
        records = store.read_activity()
        assert [(r.kind, r.value) for r in records] == [
            ("focus", "vim-scroll cluster"),
            ("status", "blocked"),
        ]
        assert records[0].agent_id == "agent-a"
        assert records[0].name == "kodemirror"  # name captured for the digest

    def test_noop_reset_is_not_logged(self, store: StateStore) -> None:
        self._reg(store, "agent-a", "a")
        store.set_activity_log(True)
        store.set_focus("agent-a", "same")
        store.set_focus("agent-a", "same")  # unchanged → no record
        store.set_status("agent-a", "working")
        store.set_status("agent-a", "working")  # unchanged → no record
        assert [r.kind for r in store.read_activity()] == ["focus", "status"]

    def test_clearing_focus_logs_null(self, store: StateStore) -> None:
        self._reg(store, "agent-a", "a")
        store.set_activity_log(True)
        store.set_focus("agent-a", "x")
        store.set_focus("agent-a", "")  # clear
        last = store.read_activity()[-1]
        assert last.kind == "focus"
        assert last.value is None

    def test_read_filters_by_since(self, store: StateStore) -> None:
        self._reg(store, "agent-a", "a")
        store.set_activity_log(True)
        store.set_focus("agent-a", "x")
        assert len(store.read_activity(since=datetime(2000, 1, 1, tzinfo=UTC))) == 1
        assert store.read_activity(since=datetime(2999, 1, 1, tzinfo=UTC)) == []

    def test_read_filters_by_agent_id_or_name(self, store: StateStore) -> None:
        self._reg(store, "agent-a", "kodemirror")
        self._reg(store, "agent-b", "ksrpc")
        store.set_activity_log(True)
        store.set_focus("agent-a", "x")
        store.set_focus("agent-b", "y")
        assert len(store.read_activity(agent="agent-a")) == 1
        assert len(store.read_activity(agent="kodemirror")) == 1  # name also matches
        assert store.read_activity(agent="nobody") == []


# ------------------------------------------------- peer links + #55 resolution


def _peer_link(host: str, **overrides: object) -> PeerLink:
    """An attached peer record owned by this (live) process, plus overrides."""
    fields: dict[str, object] = {
        "host": host,
        "role": "connect",
        "bridge_pid": os.getpid(),
        "bridge_pid_start": store_module.pid_start_time(os.getpid()),
        "attached_at": datetime.now(UTC),
        "last_frame_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return PeerLink.model_validate(fields)


class TestPeerLinks:
    """The peer record: the state whose absence made #55 undiagnosable."""

    def test_write_read_list_round_trip(self, store: StateStore) -> None:
        store.write_peer_link(_peer_link("adolin"))
        store.write_peer_link(_peer_link("kaladin"))
        assert [link.host for link in store.list_peer_links()] == ["adolin", "kaladin"]
        found = store.read_peer_link("adolin")
        assert found is not None
        assert found.role == "connect"

    def test_unknown_host_reads_as_none(self, store: StateStore) -> None:
        assert store.read_peer_link("never-bridged") is None

    def test_attached_while_the_bridge_process_lives(self, store: StateStore) -> None:
        assert peer_link_is_attached(_peer_link("adolin"))

    def test_detached_once_the_bridge_pid_is_gone(self, store: StateStore) -> None:
        link = _peer_link("adolin", bridge_pid=_dead_pid(), bridge_pid_start=None)
        assert not peer_link_is_attached(link)

    def test_detached_when_the_start_time_no_longer_matches(self, store: StateStore) -> None:
        """PID reuse: the pid is alive but belongs to something else now."""
        link = _peer_link("adolin", bridge_pid_start=-1)
        assert not peer_link_is_attached(link)

    def test_a_clean_teardown_is_detached_even_with_a_live_pid(self, store: StateStore) -> None:
        """``detached_at`` is authoritative — the same process may reconnect later."""
        link = _peer_link("adolin", detached_at=datetime.now(UTC))
        assert not peer_link_is_attached(link)

    def test_a_host_label_that_is_a_path_is_refused(self, store: StateStore) -> None:
        with pytest.raises(ValueError, match="not a usable peer host label"):
            store.write_peer_link(_peer_link("../../etc/passwd"))

    def test_a_corrupt_record_does_not_break_the_listing(self, store: StateStore) -> None:
        """``list`` and message resolution must survive one bad file."""
        store.write_peer_link(_peer_link("adolin"))
        (store.root / "peers" / "garbage.json").write_text("{not json")
        assert [link.host for link in store.list_peer_links()] == ["adolin"]


class TestStaleRowsCannotShadowLiveAgents:
    """#55 secondary finding 4 — the one that lost messages silently."""

    def test_the_live_agent_wins_a_shared_display_name(self, store: StateStore) -> None:
        dead = _dead_pid()
        store.register_agent(name="ksrpc", working_dir="/tmp", pid=dead, agent_id="agent-dead-1")
        store.register_agent(name="ksrpc", working_dir="/tmp", pid=dead, agent_id="agent-dead-2")
        store.register_agent(
            name="ksrpc", working_dir="/tmp", pid=os.getpid(), agent_id="agent-live"
        )
        msg = store.send_message(from_agent="agent-x", to_agent="ksrpc", body="hi")
        assert msg.to_agent == "agent-live"
        # And nothing was written to either dead inbox.
        for dead_id in ("agent-dead-1", "agent-dead-2"):
            assert store.recv_messages(dead_id) == []

    def test_a_name_matching_only_stopped_sessions_is_refused(self, store: StateStore) -> None:
        dead = _dead_pid()
        store.register_agent(name="ksrpc", working_dir="/tmp", pid=dead, agent_id="agent-dead-1")
        store.register_agent(name="ksrpc", working_dir="/tmp", pid=dead, agent_id="agent-dead-2")
        with pytest.raises(ValueError) as excinfo:
            store.send_message(from_agent="agent-x", to_agent="ksrpc", body="hi")
        text = str(excinfo.value)
        assert "every agent carrying it on this bus has STOPPED" in text
        assert "address it by its exact agent_id" in text
        assert store.recv_messages("agent-dead-1") == []
        assert store.recv_messages("agent-dead-2") == []

    def test_one_stopped_session_is_refused_too(self, store: StateStore) -> None:
        """A single dead row is the same fault as two — it resolved before #55."""
        store.register_agent(
            name="ksrpc", working_dir="/tmp", pid=_dead_pid(), agent_id="agent-dead"
        )
        with pytest.raises(ValueError, match="every agent carrying it on this bus has STOPPED"):
            store.send_message(from_agent="agent-x", to_agent="ksrpc", body="hi")

    def test_the_exact_id_of_a_stopped_session_still_resolves(self, store: StateStore) -> None:
        """Deliberate: mail waits for a session that is merely restarting."""
        store.register_agent(
            name="ksrpc", working_dir="/tmp", pid=_dead_pid(), agent_id="agent-dead"
        )
        msg = store.send_message(from_agent="agent-x", to_agent="agent-dead", body="hi")
        assert msg.to_agent == "agent-dead"

    def test_ambiguity_is_only_counted_among_live_agents(self, store: StateStore) -> None:
        store.register_agent(name="dup", working_dir="/tmp", pid=_dead_pid(), agent_id="agent-dead")
        store.register_agent(name="dup", working_dir="/tmp", pid=os.getpid(), agent_id="agent-a")
        store.register_agent(name="dup", working_dir="/tmp", pid=os.getpid(), agent_id="agent-b")
        with pytest.raises(ValueError) as excinfo:
            store.send_message(from_agent="agent-x", to_agent="dup", body="hi")
        text = str(excinfo.value)
        assert "display name shared by multiple live agents" in text
        assert "agent-dead" not in text


class TestDeliveryVerdict:
    """Appending is not delivering, and the caller has to be told which happened."""

    def test_a_live_local_session_is_a_delivery(self, store: StateStore) -> None:
        store.register_agent(name="a", working_dir="/tmp", pid=os.getpid(), agent_id="agent-a")
        live, detail = store.delivery_verdict("agent-a")
        assert live
        assert "whose session is running" in detail

    def test_a_stopped_session_is_not_a_delivery(self, store: StateStore) -> None:
        store.register_agent(name="a", working_dir="/tmp", pid=_dead_pid(), agent_id="agent-a")
        live, detail = store.delivery_verdict("agent-a")
        assert not live
        assert detail.startswith("NOT DELIVERED TO A LIVE SESSION")
        assert "treat this as QUEUED, not delivered" in detail

    def test_an_unregistered_inbox_is_not_a_delivery(self, store: StateStore) -> None:
        live, detail = store.delivery_verdict("agent-nobody")
        assert not live
        assert detail.startswith("NOT DELIVERED TO A LIVE SESSION")

    def test_a_mirrored_agent_behind_an_attached_bridge_is_a_delivery(
        self, store: StateStore
    ) -> None:
        store.write_peer_link(_peer_link("adolin", last_registry_at=datetime.now(UTC)))
        store.sync_remote_agents(
            "adolin",
            [
                Agent(
                    agent_id="agent-r",
                    name="remote",
                    working_dir="/tmp",
                    pid=1,
                    last_seen=datetime.now(UTC),
                )
            ],
            os.getpid(),
            store_module.pid_start_time(os.getpid()),
        )
        live, detail = store.delivery_verdict("agent-r@adolin")
        assert live
        assert "Queued for the bridge to 'adolin'" in detail

    def test_a_mirrored_agent_with_no_bridge_is_not_a_delivery(self, store: StateStore) -> None:
        store.sync_remote_agents(
            "adolin",
            [
                Agent(
                    agent_id="agent-r",
                    name="remote",
                    working_dir="/tmp",
                    pid=1,
                    last_seen=datetime.now(UTC),
                )
            ],
            os.getpid(),
            store_module.pid_start_time(os.getpid()),
        )
        live, detail = store.delivery_verdict("agent-r@adolin")
        assert not live
        assert detail.startswith("NOT DELIVERED TO A LIVE SESSION")
        assert "will be forwarded when `spanreed conjoin adolin` next attaches" in detail


class TestResolverNamesTheRightRemedy:
    """#55 secondary finding 1.

    Every assertion here is on a LONG phrase. The reported failure was a human
    reading a short, true, useless sentence and acting on it for three days, so
    the text is the artifact under test; a substring like "list_agents" would
    match all six of these messages and distinguish none of them.
    """

    UNQUALIFIED_REMEDY = "Call list_agents to find the recipient's agent_id"

    def _error(self, store: StateStore, to_agent: str) -> str:
        with pytest.raises(ValueError) as excinfo:
            store.send_message(from_agent="agent-x", to_agent=to_agent, body="hi")
        return str(excinfo.value)

    def test_an_unknown_bare_id_still_points_at_list_agents(self, store: StateStore) -> None:
        text = self._error(store, "agent-typo")
        assert "is not a registered agent_id or display name on this host" in text
        assert self.UNQUALIFIED_REMEDY in text
        assert "address it as <agent_id>@<host>" in text

    def test_a_host_with_no_bridge_says_there_is_no_bridge(self, store: StateStore) -> None:
        text = self._error(store, "agent-r@adolin")
        assert "This bus has NO BRIDGE to 'adolin'" in text
        assert "no `spanreed conjoin` has ever attached that host here" in text
        assert "This is NOT evidence that 'agent-r' has stopped" in text

    def test_an_attached_but_never_synced_bridge_says_so(self, store: StateStore) -> None:
        """The exact fault of #55: everything healthy, nothing addressable."""
        store.write_peer_link(_peer_link("adolin", registry_requests_sent=7))
        text = self._error(store, "agent-r@adolin")
        assert "has NEVER RECEIVED A REGISTRY SNAPSHOT from 'adolin'" in text
        assert "Registry sync and message transport are independent paths" in text
        assert "This is NOT evidence that 'agent-r' has stopped" in text
        assert "We have asked 'adolin' to advertise 7 time(s)" in text

    def test_the_never_synced_error_does_not_send_you_to_list_agents(
        self, store: StateStore
    ) -> None:
        """The specific wrong remedy. list_agents returns nothing for that host,
        which *confirms* the false belief that the agent is gone."""
        store.write_peer_link(_peer_link("adolin"))
        text = self._error(store, "agent-r@adolin")
        assert self.UNQUALIFIED_REMEDY not in text

    def test_a_dead_bridge_says_the_bridge_is_detached(self, store: StateStore) -> None:
        store.write_peer_link(_peer_link("adolin", bridge_pid=_dead_pid(), bridge_pid_start=None))
        text = self._error(store, "agent-r@adolin")
        assert "The bridge to 'adolin' is DETACHED" in text
        assert "until the bridge is restarted (`spanreed conjoin adolin`)" in text
        assert self.UNQUALIFIED_REMEDY not in text

    def test_a_peer_advertising_nothing_points_at_the_peer(self, store: StateStore) -> None:
        store.write_peer_link(
            _peer_link(
                "adolin",
                last_registry_at=datetime.now(UTC),
                last_registry_agents=0,
                peer_registry_rows=4,
                peer_stale_rows=4,
                registry_syncs=12,
            )
        )
        text = self._error(store, "agent-r@adolin")
        assert "that snapshot advertised ZERO AGENTS" in text
        assert "the registry on adolin held 4 local row(s), 4 of which" in text
        assert "the fix belongs on 'adolin'" in text

    def test_a_healthy_bridge_calls_it_a_genuinely_unknown_agent(self, store: StateStore) -> None:
        store.write_peer_link(
            _peer_link(
                "adolin",
                last_registry_at=datetime.now(UTC),
                last_registry_agents=1,
                registry_syncs=12,
            )
        )
        store.sync_remote_agents(
            "adolin",
            [
                Agent(
                    agent_id="agent-real",
                    name="real",
                    working_dir="/tmp",
                    pid=1,
                    last_seen=datetime.now(UTC),
                )
            ],
            os.getpid(),
            store_module.pid_start_time(os.getpid()),
        )
        text = self._error(store, "agent-ghost@adolin")
        assert "is a genuinely unknown agent rather than a sync failure" in text
        assert "'adolin' is currently advertising: agent-real@adolin" in text

    def test_the_six_messages_are_actually_distinct(self, store: StateStore) -> None:
        """A guard on the guards: six branches that all said the same thing would
        pass every assertion above that looks for a shared phrase."""
        messages: list[str] = []
        messages.append(self._error(store, "agent-typo"))
        messages.append(self._error(store, "agent-r@adolin"))  # no bridge
        store.write_peer_link(_peer_link("adolin"))
        messages.append(self._error(store, "agent-r@adolin"))  # never synced
        store.write_peer_link(
            _peer_link("adolin", last_registry_at=datetime.now(UTC), last_registry_agents=0)
        )
        messages.append(self._error(store, "agent-r@adolin"))  # zero agents
        store.write_peer_link(
            _peer_link("adolin", last_registry_at=datetime.now(UTC), last_registry_agents=3)
        )
        messages.append(self._error(store, "agent-r@adolin"))  # healthy, unknown id
        store.write_peer_link(_peer_link("adolin", bridge_pid=_dead_pid(), bridge_pid_start=None))
        messages.append(self._error(store, "agent-r@adolin"))  # detached
        assert len(set(messages)) == 6


class TestFormatAge:
    def test_none_reads_as_never(self) -> None:
        assert format_age(None) == "never"

    def test_seconds_minutes_hours_days(self) -> None:
        now = datetime(2026, 1, 2, tzinfo=UTC)
        assert format_age(now - timedelta(seconds=4), now=now) == "4s ago"
        assert format_age(now - timedelta(minutes=2, seconds=3), now=now) == "2m 3s ago"
        assert format_age(now - timedelta(hours=1, minutes=3), now=now) == "1h 3m ago"
        assert format_age(now - timedelta(days=2, hours=5), now=now) == "2d 5h ago"

    def test_a_future_timestamp_says_check_the_clocks(self) -> None:
        now = datetime(2026, 1, 2, tzinfo=UTC)
        assert "check the clocks on both hosts" in format_age(now + timedelta(hours=1), now=now)
