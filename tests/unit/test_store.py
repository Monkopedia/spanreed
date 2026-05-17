"""Tests for the filesystem-backed StateStore."""

from __future__ import annotations

import os
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spanreed import store as store_module
from spanreed.protocol import Agent
from spanreed.store import StateStore


def _dead_pid() -> int:
    """Spawn-and-reap a subprocess to get a PID guaranteed not in use."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


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

    def test_touch_updates_last_seen(self, store: StateStore) -> None:
        agent = store.register_agent(name="alice", working_dir="/x", pid=os.getpid())
        original_seen = agent.last_seen
        time.sleep(0.01)
        store.touch_agent(agent.agent_id)
        refreshed = next(a for a in store.list_agents() if a.agent_id == agent.agent_id)
        assert refreshed.last_seen > original_seen

    def test_touch_unknown_is_noop(self, store: StateStore) -> None:
        store.touch_agent("agent-does-not-exist")

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


# ----------------------------------------------------------------- staleness via TTL


class TestStaleness:
    def test_stale_if_pid_dead(self) -> None:
        agent = Agent(
            agent_id="x",
            name="x",
            working_dir="/x",
            pid=_dead_pid(),
            last_seen=datetime.now(UTC),
        )
        assert store_module.is_stale(agent, datetime.now(UTC))

    def test_stale_if_last_seen_too_old(self) -> None:
        agent = Agent(
            agent_id="x",
            name="x",
            working_dir="/x",
            pid=os.getpid(),
            last_seen=datetime.now(UTC) - timedelta(hours=2),
        )
        assert store_module.is_stale(agent, datetime.now(UTC))

    def test_fresh_with_live_pid(self) -> None:
        agent = Agent(
            agent_id="x",
            name="x",
            working_dir="/x",
            pid=os.getpid(),
            last_seen=datetime.now(UTC),
        )
        assert not store_module.is_stale(agent, datetime.now(UTC))


# ----------------------------------------------------------------- inboxes


class TestInboxes:
    def test_send_creates_inbox_if_missing(self, store: StateStore) -> None:
        msg = store.send_message(from_agent="A", to_agent="B", body="hello")
        assert msg.msg_id.startswith("msg-")
        assert msg.body == "hello"
        assert store.recv_messages("B") == [msg]

    def test_recv_returns_empty_for_unknown_agent(self, store: StateStore) -> None:
        assert store.recv_messages("nobody") == []

    def test_send_then_recv_round_trip(self, store: StateStore) -> None:
        m1 = store.send_message(from_agent="A", to_agent="B", body="one")
        m2 = store.send_message(from_agent="A", to_agent="B", body="two")
        assert store.recv_messages("B") == [m1, m2]

    def test_in_reply_to_round_trip(self, store: StateStore) -> None:
        m1 = store.send_message(from_agent="A", to_agent="B", body="ping")
        m2 = store.send_message(from_agent="B", to_agent="A", body="pong", in_reply_to=m1.msg_id)
        delivered = store.recv_messages("A")
        assert delivered == [m2]
        assert delivered[0].in_reply_to == m1.msg_id


# ----------------------------------------------------------------- cursors


class TestCursors:
    def test_recv_with_cursor_returns_only_new(self, store: StateStore) -> None:
        m1 = store.send_message(from_agent="A", to_agent="B", body="one")
        m2 = store.send_message(from_agent="A", to_agent="B", body="two")
        m3 = store.send_message(from_agent="A", to_agent="B", body="three")
        assert store.recv_messages("B", since_msg_id=m1.msg_id) == [m2, m3]

    def test_recv_with_unknown_cursor_returns_all(self, store: StateStore) -> None:
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
    def test_returns_matching_reply(self, store: StateStore) -> None:
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

    def test_times_out_when_no_reply(self, store: StateStore) -> None:
        m1 = store.send_message(from_agent="A", to_agent="B", body="ping")
        result = store.wait_for_reply(
            agent_id="A",
            in_reply_to=m1.msg_id,
            timeout_s=0.2,
            poll_interval_s=0.05,
        )
        assert result is None

    def test_returns_existing_matching_message_immediately(self, store: StateStore) -> None:
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

    def test_ignores_unrelated_messages(self, store: StateStore) -> None:
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
