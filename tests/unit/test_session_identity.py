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
from spanreed.store import StateStore, is_stale, pid_start_time


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

    def test_a_recycled_pid_cannot_hand_over_a_dead_agents_identity(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reuse guard, which an earlier revision of this resolver discarded.

        ``$CLAUDE_PID`` is our own ancestor, so it is alive by construction —
        which means a matching entry can only be stale by ``pid_start``
        mismatch, i.e. *precisely* the pid-reuse case. Consulting the
        include-stale view therefore admitted nothing but this: an abandoned
        agent whose pid the OS later handed to a live session was adopted as
        that session's identity, and the drift warning asserted it was right.
        """
        abandoned = tmp_path / "abandoned-repo"
        abandoned.mkdir()
        mine = tmp_path / "my-repo"
        mine.mkdir()

        dead_id = _register(bus, abandoned, pid=os.getpid())
        # Forge reuse: the recorded start time no longer matches the live pid's.
        entry = bus.list_agents(include_stale=True)[0]
        entry.pid_start = (entry.pid_start or 0) + 999_999
        bus._write_registry_unlocked([entry])  # pyright: ignore[reportPrivateUsage]
        assert is_stale(bus.list_agents(include_stale=True)[0])
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))

        agent_id, _, warning = session_agent_identity(mine)

        assert agent_id != dead_id
        assert agent_id == derive_agent_identity(mine)[0]
        assert warning is None

    def test_a_restarting_session_is_not_why_stale_entries_were_consulted(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Documents why nothing was lost by refusing stale entries.

        A session mid-restart cannot match on pid anyway: its registry entry
        still holds the *old*, dead pid, while ``$CLAUDE_PID`` is the new
        process.

        Honest about its own weight: this test does NOT pin that claim. It is
        green under both implementations, because ``owned`` is empty either way
        — a negative with no observable to assert on. The reuse test above is
        the one that discriminates. Kept because the reasoning is what stops
        someone restoring the include-stale view, and a reader who finds only
        the reuse test will not know the mid-restart case was considered.
        """
        home = tmp_path / "repo"
        home.mkdir()
        old_pid = 2**31 - 1  # above /proc/sys/kernel/pid_max on any sane host
        _register(bus, home, pid=old_pid)
        restarted_pid = os.getpid()
        monkeypatch.setenv("CLAUDE_PID", str(restarted_pid))

        # No entry claims the new pid, so we fall back — exactly as before the
        # anchor existed, and exactly until the hook re-registers.
        assert session_agent_identity(home) == (*derive_agent_identity(home), None)


class TestTheGuardSaysWhenItCouldNotRun:
    """Rule 7, on the residual #31 tracks.

    The staleness filter leans on ``pid_start`` to catch pid reuse. Where none
    was recorded — macOS has no ``/proc`` — it cannot run, and the match rests
    on the bare pid. The warning that overrides a visible answer should not
    sound more certain than it is.
    """

    def _register_without_start_time(self, bus: StateStore, wd: Path, pid: int) -> str:
        agent_id, name = derive_agent_identity(wd)
        bus.register_agent(name=name, working_dir=str(wd), pid=pid, agent_id=agent_id)
        entry = bus.list_agents(include_stale=True)[0]
        entry.pid_start = None  # what registration produces with no /proc
        bus._write_registry_unlocked([entry])  # pyright: ignore[reportPrivateUsage]
        return agent_id

    def test_drift_warning_admits_the_reuse_check_could_not_run(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "repo"
        home.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        registered = self._register_without_start_time(bus, home, pid=os.getpid())
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))

        agent_id, _, warning = session_agent_identity(elsewhere)

        assert agent_id == registered  # still resolved — see below
        assert warning is not None
        assert "could not run" in warning

    def test_a_missing_start_time_does_not_refuse_the_match(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deliberately NOT treated as disqualifying.

        On macOS *every* entry has no start time, so refusing these would hand
        that platform back the unconditional bug — a certainty, traded away to
        avoid a conjunction. Warn, resolve, and track the gap in #31.
        """
        home = tmp_path / "repo"
        home.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        registered = self._register_without_start_time(bus, home, pid=os.getpid())
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))

        assert session_agent_identity(elsewhere)[0] == registered
        assert session_agent_identity(elsewhere)[0] != derive_agent_identity(elsewhere)[0]

    @pytest.mark.skipif(
        pid_start_time(os.getpid()) is None,
        reason="no process start time available here — which is #31's gap, and on"
        " such a platform the note is *correct* on every drift, so there is"
        " nothing to discriminate",
    )
    def test_no_note_when_the_check_DID_run(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The negative this class was missing, and the reason it mattered.

        Three mutations proved the note *appears*; none proved it appears
        **only** when the guard could not run. With that unpinned, folding the
        note into the warning unconditionally kept the whole suite green — and
        every Linux drift warning would then claim the reuse check could not run
        on a machine where it ran and passed. A message asserting something the
        code did not verify is precisely what this note was added to stop, so
        leaving its converse untested reintroduced the defect in mirror image.
        """
        home = tmp_path / "repo"
        home.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        _register(bus, home, pid=os.getpid())  # ordinary registration: start time recorded
        assert bus.list_agents()[0].pid_start is not None
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))

        _, _, warning = session_agent_identity(elsewhere)

        assert warning is not None, "this is still a drift — the warning must fire"
        assert "could not run" not in warning
        assert "#31" not in warning

    def test_no_note_when_the_answers_agree(
        self, bus: StateStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing is riding on the match when it agrees with the directory, and
        macOS would otherwise warn on every single command."""
        home = tmp_path / "repo"
        home.mkdir()
        self._register_without_start_time(bus, home, pid=os.getpid())
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))

        assert session_agent_identity(home)[2] is None


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
