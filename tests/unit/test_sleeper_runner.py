"""Tests for waking a sleeper and for its runner (stage 2).

No test here starts a real tmux session or a real Claude: the launcher is
replaced, and the runner's Claude is a small script.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from spanreed import cli, sleeper_runner
from spanreed.protocol import Message
from spanreed.sleepers import InboxPosition, SleeperRecord
from spanreed.store import StateStore, pid_start_time
from spanreed.transcript import Deliveries, TranscriptStatus

SID = "00000000-0000-4000-8000-0000000000aa"


def _record(name: str = "nap", **overrides: object) -> SleeperRecord:
    fields: dict[str, object] = {
        "name": name,
        "agent_id": f"agent-{name}",
        "cwd": "/tmp",
        "session_id": SID,
        "permission_mode": "acceptEdits",
        "claude_path": "/usr/bin/claude",
        "spanreed_path": "/usr/bin/spanreed",
        "path_env": "/usr/bin",
        "created_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return SleeperRecord.model_validate(fields)


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


def _update(store: StateStore, name: str, **update: object) -> None:
    with store.sleepers.lock(name):
        record = store.sleepers.read(name)
        assert record is not None
        store.sleepers.write(record.model_copy(update=update))


def _read(store: StateStore, name: str = "nap") -> SleeperRecord:
    record = store.sleepers.read(name)
    assert record is not None
    return record


Launches = list[SleeperRecord]


@pytest.fixture
def launches(monkeypatch: pytest.MonkeyPatch, store: StateStore) -> Launches:
    """Replace tmux: record each launch, as the record stood on disk at that moment,
    and report this process as the pane (so the runner reads as alive)."""
    seen: Launches = []

    def fake(_store: StateStore, record: SleeperRecord) -> int:
        on_disk = store.sleepers.read(record.name)
        assert on_disk is not None
        seen.append(on_disk)
        return os.getpid()

    monkeypatch.setattr(sleeper_runner, "_launch_runner", fake)
    return seen


class TestWakeIfNeeded:
    def test_not_a_sleeper(self, store: StateStore, launches: Launches) -> None:
        assert sleeper_runner.wake_if_needed(store, "agent-someone") is None
        assert launches == []

    def test_an_asleep_sleeper_is_launched(self, store: StateStore, launches: Launches) -> None:
        store.create_sleeper(_record())
        outcome = sleeper_runner.wake_if_needed(store, "agent-nap")
        assert outcome is not None and outcome.launched
        record = _read(store)
        assert (record.state, record.generation) == ("awake", 1)
        assert record.tmux_session == "spanreed-nap-1"
        assert record.runner_pid == os.getpid()
        assert record.runner_pid_start == pid_start_time(os.getpid())

    def test_the_generation_is_on_disk_before_the_launch(
        self, store: StateStore, launches: Launches
    ) -> None:
        """A waker dying after new-session must not leave the next one reusing the name."""
        store.create_sleeper(_record())
        sleeper_runner.wake_if_needed(store, "agent-nap")
        [at_launch] = launches
        assert (at_launch.generation, at_launch.tmux_session) == (1, "spanreed-nap-1")
        assert at_launch.state == "asleep"

    def test_an_awake_sleeper_with_a_live_runner_is_left_alone(
        self, store: StateStore, launches: Launches
    ) -> None:
        store.create_sleeper(_record())
        sleeper_runner.wake_if_needed(store, "agent-nap")
        outcome = sleeper_runner.wake_if_needed(store, "agent-nap")
        assert outcome is not None and not outcome.launched
        assert "already awake" in outcome.reason
        assert len(launches) == 1

    @pytest.mark.parametrize("state", ["awake", "sleeping"])
    def test_a_dead_runner_is_woken_again_under_a_new_session_name(
        self, store: StateStore, launches: Launches, state: str
    ) -> None:
        """Loss F: a runner that died mid-exit must not leave a sleeper nobody wakes."""
        store.create_sleeper(_record())
        _update(store, "nap", state=state, generation=4, runner_pid=_dead_pid())
        outcome = sleeper_runner.wake_if_needed(store, "agent-nap")
        assert outcome is not None and outcome.launched
        assert _read(store).tmux_session == "spanreed-nap-5"

    def test_an_orphaned_claude_is_not_resumed_twice(
        self, store: StateStore, launches: Launches
    ) -> None:
        store.create_sleeper(_record())
        _update(store, "nap", state="awake", runner_pid=_dead_pid(), claude_pid=os.getppid())
        store.register_agent(name="nap", working_dir="/tmp", pid=os.getpid(), agent_id="agent-nap")
        outcome = sleeper_runner.wake_if_needed(store, "agent-nap")
        assert outcome is not None and not outcome.launched
        assert "not resuming a second copy" in outcome.reason
        assert launches == []
        record = _read(store)
        assert record.last_wake_failure is not None
        [row] = store.list_agents()
        assert row.status == "needs_input"

    def test_a_launch_failure_is_recorded_and_never_raised(self, store: StateStore) -> None:
        """The default test fixture refuses tmux, as a missing tmux would."""
        store.create_sleeper(_record())
        msg = store.send_message("tester", "nap", "hello")
        assert msg.to_agent == "agent-nap"
        record = _read(store)
        assert record.state == "asleep"
        assert record.last_wake_failure is not None
        assert "tests do not launch tmux" in record.last_wake_failure.reason
        assert record.generation == 1, "the generation is spent even when the launch fails"

    def test_an_exception_inside_the_wake_is_reported_not_raised(
        self, store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store.create_sleeper(_record())

        def boom(*_args: object) -> int:
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(sleeper_runner, "_launch_runner", boom)
        outcome = sleeper_runner.wake_if_needed(store, "agent-nap")
        assert outcome is not None and not outcome.launched
        assert "disk on fire" in outcome.reason
        failure = _read(store).last_wake_failure
        assert failure is not None and "disk on fire" in failure.reason


class TestEveryInboxWriterWakes:
    def test_send_message(self, store: StateStore, launches: Launches) -> None:
        store.create_sleeper(_record())
        store.send_message("tester", "nap", "wake up")
        assert len(launches) == 1
        assert store.delivery_verdict("agent-nap")[0]

    def test_append_message(self, store: StateStore, launches: Launches) -> None:
        """The conjoin bridge's path: mail from a peer wakes a local sleeper."""
        store.create_sleeper(_record())
        msg = Message(
            msg_id="msg-0000beef",
            from_agent="a@hostB",
            to_agent="agent-nap",
            body="hi",
            ts=datetime.now(UTC),
        )
        store.append_message(msg)
        assert len(launches) == 1

    def test_a_deduplicated_append_does_not_wake(
        self, store: StateStore, launches: Launches
    ) -> None:
        store.create_sleeper(_record())
        msg = Message(
            msg_id="msg-0000beef",
            from_agent="a@hostB",
            to_agent="agent-nap",
            body="hi",
            ts=datetime.now(UTC),
        )
        store.append_message(msg)
        _update(store, "nap", state="asleep", runner_pid=None)
        store.append_message(msg)
        assert len(launches) == 1

    def test_mail_to_an_ordinary_agent_launches_nothing(
        self, store: StateStore, launches: Launches
    ) -> None:
        store.create_sleeper(_record())
        store.register_agent(name="b", working_dir="/", pid=os.getpid(), agent_id="agent-b")
        store.send_message("tester", "agent-b", "hi")
        assert launches == []


class TestLaunchSettings:
    def test_first_launch_uses_the_session_id(self) -> None:
        argv = sleeper_runner.claude_argv(_record(model="claude-opus-5-5"), resume=False)
        assert argv == [
            "/usr/bin/claude",
            "--session-id",
            SID,
            "--permission-mode",
            "acceptEdits",
            "--model",
            "claude-opus-5-5",
        ]

    def test_later_launches_resume_and_keep_the_mode(self) -> None:
        argv = sleeper_runner.claude_argv(_record(permission_mode="plan"), resume=True)
        assert argv == ["/usr/bin/claude", "--resume", SID, "--permission-mode", "plan"]

    def test_the_environment_is_an_allowlist(
        self, store: StateStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        for leak in ("CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "ANTHROPIC_API_KEY", "SSH_AUTH_SOCK"):
            monkeypatch.setenv(leak, "leaked")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("LC_TIME", "C")
        monkeypatch.setenv("TERM", "tmux-256color")
        env = sleeper_runner.claude_env(store, _record(path_env="/opt/bin:/usr/bin"))
        assert set(env) == {
            "PATH",
            "HOME",
            "USER",
            "LOGNAME",
            "SHELL",
            "SPANREED_AGENT_NAME",
            "SPANREED_SLEEPER",
            "SPANREED_STATE_ROOT",
            "LANG",
            "LC_TIME",
            "TERM",
        }
        assert env["PATH"] == "/opt/bin:/usr/bin"
        assert env["SPANREED_AGENT_NAME"] == "nap"
        assert env["SPANREED_STATE_ROOT"] == str(store.root)


class TestClaim:
    def test_the_named_runner_confirms(self, store: StateStore, launches: Launches) -> None:
        store.create_sleeper(_record())
        sleeper_runner.wake_if_needed(store, "agent-nap")
        claim = sleeper_runner._claim(store, "nap", 1)  # pyright: ignore[reportPrivateUsage]
        assert claim is not None and claim.runner_pid == os.getpid()

    def test_a_stale_generation_exits(self, store: StateStore, launches: Launches) -> None:
        store.create_sleeper(_record())
        sleeper_runner.wake_if_needed(store, "agent-nap")
        assert sleeper_runner._claim(store, "nap", 0) is None  # pyright: ignore[reportPrivateUsage]

    def test_another_live_runner_wins(self, store: StateStore) -> None:
        store.create_sleeper(_record())
        _update(store, "nap", state="awake", generation=1, runner_pid=os.getppid())
        assert sleeper_runner._claim(store, "nap", 1) is None  # pyright: ignore[reportPrivateUsage]

    def test_a_record_whose_waker_died_is_claimed(self, store: StateStore) -> None:
        """The waker spent the generation and died before writing the runner pid."""
        store.create_sleeper(_record())
        _update(store, "nap", generation=1, runner_pid=_dead_pid())
        claim = sleeper_runner._claim(store, "nap", 1)  # pyright: ignore[reportPrivateUsage]
        assert claim is not None and claim.state == "awake"


def _deliveries(*ids: str, real: int = 1, errors: dict[str, int] | None = None) -> Deliveries:
    return Deliveries(
        TranscriptStatus.OK, delivered=set(ids), real_turns=real, error_turns=errors or {}
    )


AfterExit = Callable[[Deliveries, Deliveries, int], "SleeperRecord | None"]


@pytest.fixture
def after_exit(store: StateStore) -> AfterExit:
    def call(before: Deliveries, after: Deliveries, returncode: int) -> SleeperRecord | None:
        return sleeper_runner._after_exit(store, "nap", before, after, returncode)  # pyright: ignore[reportPrivateUsage]

    store.create_sleeper(_record())
    _update(store, "nap", state="awake", runner_pid=os.getpid(), claude_pid=os.getpid())
    return call


def _mail(store: StateStore, *bodies: str) -> list[str]:
    return [store.send_message("tester", "agent-nap", b).msg_id for b in bodies]


class TestAfterClaudeExits:
    def test_everything_read_sleeps_and_moves_read_through(
        self, store: StateStore, launches: Launches, after_exit: AfterExit
    ) -> None:
        ids = _mail(store, "one", "two")
        assert after_exit(_deliveries(real=1), _deliveries(*ids, real=3), 0) is None
        record = _read(store)
        assert record.state == "asleep"
        assert (record.read_through.line, record.read_through.msg_id) == (2, ids[1])
        assert record.claude_pid is None

    def test_unread_mail_relaunches(
        self, store: StateStore, launches: Launches, after_exit: AfterExit
    ) -> None:
        ids = _mail(store, "one", "two")
        again = after_exit(_deliveries(real=1), _deliveries(ids[0], real=2), 0)
        assert again is not None and again.state == "awake"
        record = _read(store)
        assert (record.read_through.line, record.undelivered_line) == (1, 2)
        assert record.undelivered_attempts == 1

    @pytest.mark.parametrize("error", ["rate_limit", "authentication_failed"])
    def test_api_errors_with_no_real_turn_are_a_failed_wake(
        self, store: StateStore, launches: Launches, after_exit: AfterExit, error: str
    ) -> None:
        _mail(store, "one")
        assert after_exit(_deliveries(real=4), _deliveries(real=4, errors={error: 2}), 0) is None
        record = _read(store)
        assert record.state == "asleep"
        assert record.last_wake_failure is not None
        assert f"2 {error}" in record.last_wake_failure.reason
        assert record.read_through.line == 0, "the mail stays unread"
        assert record.undelivered_attempts == 0, "a failed wake is not a failed delivery"
        assert [a.status for a in store.list_agents()] == ["needs_input"]

    def test_a_quick_nonzero_exit_is_a_failed_wake(
        self, store: StateStore, launches: Launches, after_exit: AfterExit
    ) -> None:
        _mail(store, "one")
        assert after_exit(_deliveries(real=0), _deliveries(real=0), 1) is None
        failure = _read(store).last_wake_failure
        assert failure is not None and "Claude exited 1" in failure.reason

    def test_older_errors_do_not_make_a_working_launch_fail(
        self, store: StateStore, launches: Launches, after_exit: AfterExit
    ) -> None:
        ids = _mail(store, "one")
        before = _deliveries(real=2, errors={"rate_limit": 5})
        after_exit(before, _deliveries(*ids, real=3, errors={"rate_limit": 5}), 0)
        assert _read(store).last_wake_failure is None

    def test_an_unreadable_transcript_sleeps_without_relaunching(
        self, store: StateStore, launches: Launches, after_exit: AfterExit
    ) -> None:
        """Fail safe: relaunching would replay the backlog on every check."""
        _mail(store, "one")
        after = Deliveries(TranscriptStatus.UNRECOGNIZED)
        assert after_exit(_deliveries(real=1), after, 0) is None
        record = _read(store)
        assert record.state == "asleep"
        assert record.read_through.line == 0
        assert record.last_wake_failure is not None
        assert "unrecognized" in record.last_wake_failure.reason

    def test_a_poison_line_is_skipped_after_three_launches_where_the_model_ran(
        self, store: StateStore, launches: Launches, after_exit: AfterExit
    ) -> None:
        ids = _mail(store, "poison", "fine")
        for attempt in (1, 2):
            again = after_exit(_deliveries(real=attempt), _deliveries(real=attempt + 1), 0)
            assert again is not None
            assert _read(store).undelivered_attempts == attempt
        again = after_exit(_deliveries(real=3), _deliveries(ids[1], real=4), 0)
        assert again is None, "after the skip, everything else is read"
        record = _read(store)
        assert [(s.line, s.msg_id) for s in record.skipped_lines] == [(1, ids[0])]
        assert record.read_through.line == 2
        assert [a.status for a in store.list_agents()] == ["needs_input"]

    def test_launches_where_the_model_did_not_run_do_not_count(
        self, store: StateStore, launches: Launches, after_exit: AfterExit
    ) -> None:
        """A rate-limit window must not skip backlog one message at a time."""
        _mail(store, "one")
        for _ in range(5):
            after_exit(_deliveries(real=7), _deliveries(real=7), 0)
            _update(store, "nap", state="awake")
        record = _read(store)
        assert record.skipped_lines == []
        assert record.undelivered_attempts == 0

    def test_a_retired_sleeper_stops_the_runner(
        self, store: StateStore, after_exit: AfterExit
    ) -> None:
        _update(store, "nap", state="asleep")
        store.retire_sleeper("nap")
        assert after_exit(_deliveries(), _deliveries(), 0) is None


def _fake_claude(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "fake-claude"
    script.write_text(f"#!{sys.executable}\nimport sys, os\n{body}\n")
    script.chmod(0o755)
    return script


class TestRun:
    @pytest.fixture
    def config_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        config = tmp_path / "claude-config"
        (config / "projects" / "-tmp").mkdir(parents=True)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
        return config

    def test_a_launch_with_no_mail_sleeps(
        self, store: StateStore, launches: Launches, tmp_path: Path, config_dir: Path
    ) -> None:
        claude = _fake_claude(tmp_path, "sys.exit(0)")
        store.create_sleeper(_record(claude_path=str(claude)))
        sleeper_runner.wake_if_needed(store, "agent-nap")
        assert sleeper_runner.run(store, "nap", 1) == 0
        record = _read(store)
        assert (record.state, record.claude_pid) == ("asleep", None)

    def test_a_missing_claude_is_a_failed_wake(
        self, store: StateStore, launches: Launches, tmp_path: Path, config_dir: Path
    ) -> None:
        store.create_sleeper(_record(claude_path=str(tmp_path / "no-such-claude")))
        sleeper_runner.wake_if_needed(store, "agent-nap")
        assert sleeper_runner.run(store, "nap", 1) == 1
        failure = _read(store).last_wake_failure
        assert failure is not None and "could not launch" in failure.reason

    def test_claude_gets_the_resume_flag_once_a_transcript_exists(
        self, store: StateStore, launches: Launches, tmp_path: Path, config_dir: Path
    ) -> None:
        argv_log = tmp_path / "argv.json"
        claude = _fake_claude(
            tmp_path,
            f"import json\nopen({str(argv_log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')",
        )
        store.create_sleeper(_record(claude_path=str(claude)))
        sleeper_runner.wake_if_needed(store, "agent-nap")
        sleeper_runner.run(store, "nap", 1)
        (config_dir / "projects" / "-tmp" / f"{SID}.jsonl").write_text(
            json.dumps({"type": "user", "uuid": "u1", "message": {"content": "hi"}}) + "\n"
        )
        sleeper_runner.wake_if_needed(store, "agent-nap")
        sleeper_runner.run(store, "nap", 2)
        first, second = (json.loads(line) for line in argv_log.read_text().splitlines())
        assert first[:2] == ["--session-id", SID]
        assert second[:2] == ["--resume", SID]

    def test_a_runner_for_an_old_generation_launches_nothing(
        self, store: StateStore, launches: Launches, tmp_path: Path, config_dir: Path
    ) -> None:
        marker = tmp_path / "ran"
        claude = _fake_claude(tmp_path, f"open({str(marker)!r}, 'w').write('x')")
        store.create_sleeper(_record(claude_path=str(claude)))
        sleeper_runner.wake_if_needed(store, "agent-nap")
        assert sleeper_runner.run(store, "nap", 7) == 0
        assert not marker.exists()


class TestMonitorReplay:
    """``spanreed inbox-watch`` in a sleeper hands over unread mail, then tails."""

    @pytest.fixture
    def watch(
        self, monkeypatch: pytest.MonkeyPatch, state_root: Path, tmp_path: Path
    ) -> Callable[[], tuple[list[str], list[str]]]:
        monkeypatch.setenv("SPANREED_STATE_ROOT", str(state_root))
        monkeypatch.setenv("SPANREED_SLEEPER", "1")
        monkeypatch.setenv("SPANREED_AGENT_NAME", "nap")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
        monkeypatch.setattr(cli, "REPLAY_PACE_S", 0)
        printed: list[str] = []
        execs: list[list[str]] = []

        def fake_exec(_file: str, argv: list[str]) -> None:
            execs.append(argv)

        monkeypatch.setattr(cli.os, "execvp", fake_exec)

        def fake_print(*args: object, **_kwargs: object) -> None:
            printed.append(str(args[0]))

        monkeypatch.setattr("builtins.print", fake_print)

        def call() -> tuple[list[str], list[str]]:
            cli.main(["inbox-watch"])
            return printed, execs[-1]

        return call

    def _transcript(self, tmp_path: Path, *ids: str) -> None:
        project = tmp_path / "claude-config" / "projects" / "-tmp"
        project.mkdir(parents=True, exist_ok=True)
        event = "\n".join(json.dumps({"msg_id": i}, separators=(",", ":")) for i in ids)
        records: list[dict[str, Any]] = [
            {
                "type": "user",
                "uuid": "u1",
                "parentUuid": None,
                "message": {"content": f"<task-notification>\n<event>{event}</event>"},
            },
            {
                "type": "assistant",
                "uuid": "u2",
                "parentUuid": "u1",
                "message": {"model": "claude-opus-5-5", "content": []},
            },
        ]
        (project / f"{SID}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))

    def test_replays_only_what_claude_does_not_have_then_tails_after_it(
        self, store: StateStore, tmp_path: Path, watch: Callable[[], tuple[list[str], list[str]]]
    ) -> None:
        store.create_sleeper(_record())
        ids = [store.send_message("tester", "agent-nap", b).msg_id for b in ("a", "b", "c")]
        self._transcript(tmp_path, ids[1])  # b was read through recv after its event dropped
        printed, argv = watch()
        assert [json.loads(p)["msg_id"] for p in printed] == [ids[0], ids[2]]
        assert argv[:3] == ["tail", "-n", "+4"]

    def test_starts_after_read_through(
        self, store: StateStore, tmp_path: Path, watch: Callable[[], tuple[list[str], list[str]]]
    ) -> None:
        store.create_sleeper(_record())
        ids = [store.send_message("tester", "agent-nap", b).msg_id for b in ("a", "b")]
        _update(store, "nap", read_through=InboxPosition(line=1, msg_id=ids[0]))
        self._transcript(tmp_path)
        printed, _ = watch()
        assert [json.loads(p)["msg_id"] for p in printed] == [ids[1]]

    def test_an_unreadable_transcript_replays_everything_unread(
        self, store: StateStore, tmp_path: Path, watch: Callable[[], tuple[list[str], list[str]]]
    ) -> None:
        """Duplicates, never a loss."""
        store.create_sleeper(_record())
        ids = [store.send_message("tester", "agent-nap", b).msg_id for b in ("a", "b")]
        printed, _ = watch()  # no transcript at all
        assert [json.loads(p)["msg_id"] for p in printed] == ids

    def test_an_ordinary_session_still_tails_from_the_end(
        self,
        store: StateStore,
        monkeypatch: pytest.MonkeyPatch,
        watch: Callable[[], tuple[list[str], list[str]]],
    ) -> None:
        monkeypatch.delenv("SPANREED_SLEEPER")
        printed, argv = watch()
        assert printed == []
        assert argv[:3] == ["tail", "-n", "0"]
