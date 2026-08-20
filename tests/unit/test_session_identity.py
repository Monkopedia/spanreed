"""``session_agent_identity`` — identity follows the session, not the cwd.

The bug these pin (#23): every CLI command re-derived the agent_id from the
directory it happened to run in. A session that ``cd``\\ ed spoke on the bus
under an id that was not the one its SessionStart hook registered, so replies
routed to an inbox it does not tail. Worst case observed on the live bus: from
``~/git`` the derivation lands on ``main-coordinator``'s real id, so the drift
is not a dead letter but delivery to the wrong *live* agent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from spanreed.identity import derive_agent_identity, session_agent_identity
from spanreed.store import StateStore


@pytest.fixture
def bus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> StateStore:
    monkeypatch.setenv("SPANREED_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("SPANREED_AGENT_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    return StateStore()


def _register(bus: StateStore, wd: Path, pid: int) -> str:
    agent_id, name = derive_agent_identity(wd)
    bus.register_agent(name=name, working_dir=str(wd), pid=pid, agent_id=agent_id)
    return agent_id


class TestFollowsTheSession:
    def test_cd_does_not_change_who_you_are(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "repo"
        home.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        registered = _register(bus, home, pid=os.getpid())
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))

        agent_id, name, warning = session_agent_identity(elsewhere)

        assert agent_id == registered
        assert name == "repo"
        # The whole point: the directory answer differs, and loses.
        assert derive_agent_identity(elsewhere)[0] != registered
        assert warning is not None

    def test_no_warning_when_standing_in_your_own_directory(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "repo"
        home.mkdir()
        registered = _register(bus, home, pid=os.getpid())
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))

        agent_id, _, warning = session_agent_identity(home)

        assert agent_id == registered
        assert warning is None

    def test_drift_onto_a_live_peer_is_prevented(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The live-bus failure: the drifted-to id belongs to a real agent.

        ``~/git`` derives ``main-coordinator``'s id, so a message sent after the
        ``cd`` is not merely unaddressable — it is attributed to a peer, and the
        reply lands in *that peer's* inbox.
        """
        parent = tmp_path / "workspace"
        child = parent / "repo"
        child.mkdir(parents=True)
        peer_id = _register(bus, parent, pid=4242)
        mine = _register(bus, child, pid=os.getpid())
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))

        assert derive_agent_identity(parent)[0] == peer_id  # the collision is real
        assert session_agent_identity(parent)[0] == mine  # and no longer reachable


class TestFallsBackWhereItShould:
    def test_unregistered_session_uses_the_directory(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing registered for this pid — the hook has not run, or this is a
        human at a terminal. The old behaviour is the only answer available."""
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
        wd = tmp_path / "somewhere"
        wd.mkdir()

        assert session_agent_identity(wd) == (*derive_agent_identity(wd), None)

    def test_no_claude_pid_uses_the_directory(self, bus: StateStore, tmp_path: Path) -> None:
        wd = tmp_path / "somewhere"
        wd.mkdir()

        assert session_agent_identity(wd) == (*derive_agent_identity(wd), None)

    @pytest.mark.parametrize("junk", ["", "not-a-pid", "12x", "-1"])
    def test_unusable_claude_pid_uses_the_directory(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, junk: str
    ) -> None:
        monkeypatch.setenv("CLAUDE_PID", junk)
        wd = tmp_path / "somewhere"
        wd.mkdir()

        assert session_agent_identity(wd) == (*derive_agent_identity(wd), None)

    def test_ambiguous_pid_uses_the_directory(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two entries claiming one pid: the registry cannot say who owns it, and
        picking one would be a worse failure than the status quo."""
        a = tmp_path / "a"
        a.mkdir()
        b = tmp_path / "b"
        b.mkdir()
        _register(bus, a, pid=os.getpid())
        _register(bus, b, pid=os.getpid())
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))

        assert session_agent_identity(a) == (*derive_agent_identity(a), None)

    def test_stale_entry_still_owns_its_identity(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A session mid-restart is filtered from the default listing but has not
        stopped being itself; ``include_stale`` is why it keeps its id."""
        home = tmp_path / "repo"
        home.mkdir()
        dead_pid = 2**22 - 1  # above PID_MAX_DEFAULT: reliably not running
        registered = _register(bus, home, pid=dead_pid)
        monkeypatch.setenv("CLAUDE_PID", str(dead_pid))

        assert bus.list_agents(include_stale=False) == []
        assert session_agent_identity(tmp_path)[0] == registered


class TestOverrideStillWins:
    def test_env_override_outranks_the_registry(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "repo"
        home.mkdir()
        _register(bus, home, pid=os.getpid())
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
        monkeypatch.setenv("SPANREED_AGENT_NAME", "explicit")

        assert session_agent_identity(tmp_path) == ("agent-explicit", "explicit", None)
