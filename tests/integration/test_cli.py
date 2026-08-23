"""Tests for the spanreed CLI.

Calls ``cli.main(argv)`` with stdout captured. State root is per-test via the
``SPANREED_STATE_ROOT`` env var (so each test gets a pristine bus).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spanreed import cli
from spanreed.identity import derive_agent_identity
from spanreed.store import StateStore


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, state_root: Path, tmp_path: Path) -> Iterator[Path]:
    """Bind CLI to a per-test state root and a per-test cwd for identity derivation."""
    monkeypatch.setenv("SPANREED_STATE_ROOT", str(state_root))
    monkeypatch.delenv("SPANREED_AGENT_NAME", raising=False)
    # Inherited from the real session when the suite is run inside Claude Code.
    # Left set, it would make identity resolution depend on the developer's
    # machine; tests that care set it themselves.
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    session_cwd = tmp_path / "session-cwd"
    session_cwd.mkdir()
    monkeypatch.chdir(session_cwd)
    yield session_cwd


def _register_peer(agent_id: str) -> None:
    """Put a recipient on the registry so ``send`` accepts it (state root via env).

    ``send_message`` now rejects unregistered recipients to stop messages being
    silently dropped into an inbox no one reads.
    """
    StateStore().register_agent(
        name=agent_id, working_dir="/tmp", pid=os.getpid(), agent_id=agent_id
    )


def _run(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str]:
    rc = cli.main(argv)
    return rc, capsys.readouterr().out


# ---------------------------------------------------------------- identity


class TestIdentity:
    def test_agent_id_is_deterministic(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc1, out1 = _run(capsys, ["agent-id"])
        rc2, out2 = _run(capsys, ["agent-id"])
        assert rc1 == rc2 == 0
        assert out1.strip() == out2.strip()
        assert out1.startswith("agent-")

    def test_agent_id_changes_with_cwd(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cli_env: Path,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
    ) -> None:
        _, out1 = _run(capsys, ["agent-id"])
        other = tmp_path / "elsewhere"
        other.mkdir()
        monkeypatch.chdir(other)
        _, out2 = _run(capsys, ["agent-id"])
        assert out1.strip() != out2.strip()

    def test_env_var_overrides_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cli_env: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("SPANREED_AGENT_NAME", "alice")
        _, out = _run(capsys, ["agent-id"])
        assert out.strip() == "agent-alice"


# ---------------------------------------------------------------- register/list/deregister


class TestRegistry:
    def test_register_outputs_agent_json(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _run(capsys, ["register"])
        assert rc == 0
        agent = json.loads(out)
        assert agent["agent_id"].startswith("agent-")
        assert agent["working_dir"] == str(cli_env)

    def test_register_with_explicit_name_and_id(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _run(capsys, ["register", "--agent-id", "agent-fixed", "--name", "alice"])
        assert rc == 0
        agent = json.loads(out)
        assert agent["agent_id"] == "agent-fixed"
        assert agent["name"] == "alice"

    def test_register_then_list(self, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _run(capsys, ["register"])
        rc, out = _run(capsys, ["list"])
        assert rc == 0
        agents = json.loads(out)
        assert len(agents) == 1
        assert agents[0]["working_dir"] == str(cli_env)

    def test_deregister_removes_from_list(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, reg_out = _run(capsys, ["register"])
        agent_id = json.loads(reg_out)["agent_id"]
        rc, _ = _run(capsys, ["deregister", agent_id])
        assert rc == 0
        _, list_out = _run(capsys, ["list"])
        assert json.loads(list_out) == []

    def test_register_uses_ppid_by_default(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out = _run(capsys, ["register"])
        agent = json.loads(out)
        assert agent["pid"] == os.getppid()

    def test_register_uses_explicit_pid(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out = _run(capsys, ["register", "--pid", "12345"])
        agent = json.loads(out)
        assert agent["pid"] == 12345


# ---------------------------------------------------------------- send/recv


class TestMessages:
    def test_send_outputs_message_json(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _register_peer("agent-B")
        rc, out = _run(
            capsys,
            ["send", "--from", "agent-A", "--to", "agent-B", "--body", "hello"],
        )
        assert rc == 0
        msg = json.loads(out)
        assert msg["body"] == "hello"
        assert msg["from_agent"] == "agent-A"
        assert msg["to_agent"] == "agent-B"

    def test_send_then_recv_round_trip(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _register_peer("agent-B")
        _run(
            capsys,
            ["send", "--from", "agent-A", "--to", "agent-B", "--body", "one"],
        )
        _run(
            capsys,
            ["send", "--from", "agent-A", "--to", "agent-B", "--body", "two"],
        )
        rc, out = _run(capsys, ["recv", "agent-B"])
        assert rc == 0
        msgs = json.loads(out)
        bodies = [m["body"] for m in msgs]
        assert bodies == ["one", "two"]

    def test_send_default_from_uses_session_identity(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _register_peer("agent-B")
        _, id_out = _run(capsys, ["agent-id"])
        my_id = id_out.strip()
        _run(capsys, ["send", "--to", "agent-B", "--body", "hi"])
        _, recv_out = _run(capsys, ["recv", "agent-B"])
        msgs = json.loads(recv_out)
        assert msgs[0]["from_agent"] == my_id

    def test_send_with_in_reply_to(self, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _register_peer("agent-B")
        rc, out = _run(
            capsys,
            [
                "send",
                "--from",
                "agent-A",
                "--to",
                "agent-B",
                "--body",
                "pong",
                "--in-reply-to",
                "msg-1",
            ],
        )
        assert rc == 0
        msg = json.loads(out)
        assert msg["in_reply_to"] == "msg-1"

    def test_recv_with_since(self, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _register_peer("agent-B")
        _, m1_out = _run(
            capsys,
            ["send", "--from", "agent-A", "--to", "agent-B", "--body", "one"],
        )
        _run(
            capsys,
            ["send", "--from", "agent-A", "--to", "agent-B", "--body", "two"],
        )
        m1_id = json.loads(m1_out)["msg_id"]
        _, recv_out = _run(capsys, ["recv", "agent-B", "--since", m1_id])
        msgs = json.loads(recv_out)
        bodies = [m["body"] for m in msgs]
        assert bodies == ["two"]


# ---------------------------------------------------------------- inbox-path


class TestInboxPath:
    def test_inbox_path_for_agent_id(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _run(capsys, ["inbox-path", "agent-alice"])
        assert rc == 0
        assert out.strip().endswith("/inboxes/agent-alice.jsonl")


# ---------------------------------------------------------------- session-start


class TestSessionStart:
    def test_session_start_registers_and_emits_hook_json(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _run(capsys, ["session-start"])
        assert rc == 0
        payload = json.loads(out)
        hook = payload["hookSpecificOutput"]
        assert hook["hookEventName"] == "SessionStart"
        context = hook["additionalContext"]
        assert "Spanreed inter-agent message bus" in context
        # The agent should also now appear in the registry.
        _, list_out = _run(capsys, ["list"])
        agents = json.loads(list_out)
        assert any(a["working_dir"] == str(cli_env) for a in agents)

    def test_session_start_context_includes_identity(
        self,
        monkeypatch: pytest.MonkeyPatch,
        cli_env: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("SPANREED_AGENT_NAME", "alice")
        rc, out = _run(capsys, ["session-start"])
        assert rc == 0
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "agent-alice" in context
        assert "alice" in context

    def test_session_start_mentions_focus_capabilities(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out = _run(capsys, ["session-start"])
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "set_focus" in context
        assert "request_focus_update" in context
        assert "FOCUS_UPDATE_REQUEST" in context

    def test_session_start_writes_disposition_policy_file(
        self, cli_env: Path, state_root: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _ = _run(capsys, ["session-start"])
        assert rc == 0
        policy = (state_root / "disposition-policy.md").read_text()
        # The terse Monitor description points here; the full rules must live in it.
        assert "recv_messages" in policy
        assert "FOCUS_UPDATE_REQUEST" in policy
        assert "PushNotification" in policy

    def test_inbox_watch_writes_disposition_policy_file(
        self, cli_env: Path, state_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The Monitor command must ensure the file it points agents at exists,
        even if session-start never ran. inbox-watch execs tail, so stub execvp."""
        policy_file = state_root / "disposition-policy.md"
        assert not policy_file.exists()  # session-start did not run in this test

        captured: list[list[str]] = []

        def fake_execvp(_file: str, args: list[str]) -> None:
            captured.append(args)

        monkeypatch.setattr(cli.os, "execvp", fake_execvp)
        cli.main(["inbox-watch"])
        assert policy_file.read_text()  # written before exec
        assert captured and captured[0][0] == "tail"


# ---------------------------------------------------------------- focus


class TestFocus:
    def test_focus_show_when_unset_prints_nothing(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(capsys, ["register"])
        rc, out = _run(capsys, ["focus"])
        assert rc == 0
        assert out.strip() == ""

    def test_focus_set_then_show(self, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _run(capsys, ["register"])
        rc, _ = _run(capsys, ["focus", "implementing auth refactor"])
        assert rc == 0
        _, out = _run(capsys, ["focus"])
        assert out.strip() == "implementing auth refactor"

    def test_focus_clear(self, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _run(capsys, ["register"])
        _run(capsys, ["focus", "the focus"])
        rc, _ = _run(capsys, ["focus", "--clear"])
        assert rc == 0
        _, out = _run(capsys, ["focus"])
        assert out.strip() == ""

    def test_focus_appears_in_list_output(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(capsys, ["register"])
        _run(capsys, ["focus", "the focus"])
        _, out = _run(capsys, ["list"])
        agents = json.loads(out)
        assert agents[0]["focus"] == "the focus"

    def test_focus_set_auto_registers_if_missing(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Calling `focus` before `register` shouldn't fail — it auto-registers."""
        rc, _ = _run(capsys, ["focus", "initial focus"])
        assert rc == 0
        _, out = _run(capsys, ["list"])
        agents = json.loads(out)
        assert len(agents) == 1
        assert agents[0]["focus"] == "initial focus"

    def test_focus_preserved_across_session_start(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Re-running session-start (as the hook does on restart) preserves focus."""
        _run(capsys, ["session-start"])
        _run(capsys, ["focus", "the focus"])
        # Simulate session restart by re-running session-start.
        _run(capsys, ["session-start"])
        _, out = _run(capsys, ["focus"])
        assert out.strip() == "the focus"


# ---------------------------------------------------------------- status


class TestStatus:
    def test_status_set_then_show(self, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _run(capsys, ["register"])
        rc, _ = _run(capsys, ["status", "working"])
        assert rc == 0
        _, out = _run(capsys, ["status"])
        assert out.strip() == "working"

    def test_status_appears_in_list(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(capsys, ["register"])
        _run(capsys, ["status", "blocked"])
        _, out = _run(capsys, ["list"])
        assert json.loads(out)[0]["status"] == "blocked"

    def test_status_auto_registers_if_missing(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _ = _run(capsys, ["status", "needs_input"])
        assert rc == 0
        _, out = _run(capsys, ["list"])
        assert json.loads(out)[0]["status"] == "needs_input"

    def test_status_rejects_invalid_level(self, cli_env: Path) -> None:
        # argparse choices → SystemExit(2) on an unknown level.
        with pytest.raises(SystemExit):
            cli.main(["status", "bogus"])

    def test_status_reset_across_session_start(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Unlike focus, status is reset when the SessionStart hook re-runs."""
        _run(capsys, ["session-start"])
        _run(capsys, ["status", "blocked"])
        _run(capsys, ["session-start"])
        _, out = _run(capsys, ["status"])
        assert out.strip() == ""


class TestStatusTracking:
    def test_default_off_and_toggle(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, out = _run(capsys, ["status-tracking"])
        assert out.strip() == "off"
        _run(capsys, ["status-tracking", "on"])
        _, out = _run(capsys, ["status-tracking"])
        assert out.strip() == "on"

    def test_instruction_gated_by_flag(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        # Off: session-start context must NOT carry the status instruction.
        _, out = _run(capsys, ["session-start"])
        ctx_off = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "set_status" not in ctx_off

        # On: it must appear.
        _run(capsys, ["status-tracking", "on"])
        _, out = _run(capsys, ["session-start"])
        ctx_on = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "set_status" in ctx_on
        assert len(ctx_on) > len(ctx_off)


# ---------------------------------------------------------------- name


class TestName:
    def test_name_show_when_registered(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(capsys, ["register"])
        rc, out = _run(capsys, ["name"])
        assert rc == 0
        assert out.strip() == cli_env.name  # basename of the session cwd

    def test_name_set_then_show(self, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _run(capsys, ["register"])
        rc, _ = _run(capsys, ["name", "main-coordinator"])
        assert rc == 0
        _, out = _run(capsys, ["name"])
        assert out.strip() == "main-coordinator"

    def test_name_appears_in_list_output(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(capsys, ["register"])
        _run(capsys, ["name", "main-coordinator"])
        _, out = _run(capsys, ["list"])
        agents = json.loads(out)
        assert agents[0]["name"] == "main-coordinator"

    def test_name_preserved_across_session_start(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Re-running session-start preserves a manual rename."""
        _run(capsys, ["session-start"])
        _run(capsys, ["name", "renamed"])
        _run(capsys, ["session-start"])
        _, out = _run(capsys, ["name"])
        assert out.strip() == "renamed"

    def test_name_set_auto_registers_if_missing(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _ = _run(capsys, ["name", "fresh-name"])
        assert rc == 0
        _, out = _run(capsys, ["list"])
        agents = json.loads(out)
        assert len(agents) == 1
        assert agents[0]["name"] == "fresh-name"


# ---------------------------------------------------------------- activity log


class TestActivityLog:
    def test_activity_log_defaults_off(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _run(capsys, ["activity-log"])
        assert rc == 0
        assert out.strip() == "off"

    def test_activity_log_toggle(self, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _run(capsys, ["activity-log", "on"])
        _, out = _run(capsys, ["activity-log"])
        assert out.strip() == "on"
        _run(capsys, ["activity-log", "off"])
        _, out = _run(capsys, ["activity-log"])
        assert out.strip() == "off"

    def test_log_empty_when_no_activity(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, out = _run(capsys, ["log"])
        assert rc == 0
        assert out.strip() == ""

    def test_log_dumps_jsonl_when_enabled(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(capsys, ["activity-log", "on"])
        _run(capsys, ["focus", "vim-scroll cluster"])
        _run(capsys, ["status", "blocked"])
        rc, out = _run(capsys, ["log"])
        assert rc == 0
        records = [json.loads(line) for line in out.splitlines() if line.strip()]
        kinds = [(r["kind"], r["value"]) for r in records]
        assert ("focus", "vim-scroll cluster") in kinds
        assert ("status", "blocked") in kinds

    def test_log_filters_by_agent(self, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
        _run(capsys, ["activity-log", "on"])
        _run(capsys, ["focus", "mine"])
        _, id_out = _run(capsys, ["agent-id"])
        my_id = id_out.strip()
        _, out = _run(capsys, ["log", "--agent", my_id])
        assert len([line for line in out.splitlines() if line.strip()]) == 1
        _, none_out = _run(capsys, ["log", "--agent", "nobody"])
        assert none_out.strip() == ""

    def test_log_invalid_since_errors(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        rc, _ = _run(capsys, ["log", "--since", "5x"])
        assert rc == 2


# Aliased once so the private stays private: the parser has its own grammar
# worth pinning directly, and one alias costs a single ignore instead of one
# per call site.
_parse_since = cli._parse_since  # pyright: ignore[reportPrivateUsage]


class TestParseSince:
    """The ``--since`` grammar: a relative age, or an ISO-8601 timestamp.

    Colocated with the ``log --since`` CLI case above so all ``--since``
    behavior reads in one place. ``_parse_since`` is a pure function — no bus
    state — so these need no ``cli_env``.
    """

    @pytest.mark.parametrize(
        ("value", "delta"),
        [
            ("30m", timedelta(minutes=30)),
            ("24h", timedelta(hours=24)),
            ("7d", timedelta(days=7)),
            ("90m", timedelta(minutes=90)),
        ],
    )
    def test_relative_age_backs_off_from_now(self, value: str, delta: timedelta) -> None:
        before = datetime.now(UTC)
        parsed = _parse_since(value)
        after = datetime.now(UTC)
        # Bracket the call rather than assert an exact instant: `now` moves.
        assert before - delta <= parsed <= after - delta

    def test_iso_with_offset_preserves_instant(self) -> None:
        parsed = _parse_since("2026-07-27T12:00:00+02:00")
        assert parsed == datetime(2026, 7, 27, 10, 0, tzinfo=UTC)

    def test_iso_naive_is_coerced_to_utc(self) -> None:
        """A naive timestamp is read as UTC, not as local time."""
        parsed = _parse_since("2026-07-27T12:00:00")
        assert parsed.tzinfo is not None
        assert parsed == datetime(2026, 7, 27, 12, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        "value",
        [
            "5x",  # unknown unit
            "",  # empty
            "h",  # unit with no magnitude (len < 2, falls through to ISO)
            "90",  # magnitude with no unit (falls through to ISO)
            "-1h",  # non-digit magnitude (falls through to ISO)
            "1.5h",  # fractional magnitude is not supported
        ],
    )
    def test_malformed_raises_value_error(self, value: str) -> None:
        with pytest.raises(ValueError):
            _parse_since(value)


# ------------------------------------------------- identity follows the session


class TestIdentityFollowsTheSession:
    """#23: commands took their identity from the cwd, so a ``cd`` renamed you.

    These test the *wiring* — that each command consults
    ``session_agent_identity`` — which the unit tests for the resolver itself
    cannot show.
    """

    @pytest.fixture
    def drifted(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> str:
        """Register from the session cwd, then stand somewhere else."""
        _, out = _run(capsys, ["register"])
        registered: str = json.loads(out)["agent_id"]
        monkeypatch.setenv("CLAUDE_PID", str(os.getppid()))
        elsewhere = cli_env.parent / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        return registered

    def test_agent_id_after_cd(self, drifted: str, capsys: pytest.CaptureFixture[str]) -> None:
        _, out = _run(capsys, ["agent-id"])
        assert out.strip() == drifted

    def test_send_after_cd_is_attributed_to_the_session(
        self, drifted: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _register_peer("agent-peer")
        _run(capsys, ["send", "--to", "agent-peer", "--body", "hi"])
        inbox = StateStore().recv_messages("agent-peer")
        # Pre-fix this was the id of whatever directory the command ran in, so
        # the peer's reply went to an inbox this session does not tail.
        assert [m.from_agent for m in inbox] == [drifted]

    def test_send_from_a_neighbours_directory_is_not_attributed_to_the_neighbour(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The actual misdelivery control, end to end.

        ``test_send_after_cd_...`` walks into a directory no agent owns, so it
        only discriminates "me" from "nobody". This walks into a directory that
        a *live peer* is registered under — the real shape of the bug, where
        ``cd ~/git`` lands on main-coordinator — and asserts the message is
        attributed to the sender rather than to the neighbour whose doorstep it
        was sent from.
        """
        _, out = _run(capsys, ["register"])
        me: str = json.loads(out)["agent_id"]
        monkeypatch.setenv("CLAUDE_PID", str(os.getppid()))

        neighbour_dir = cli_env.parent / "neighbour"
        neighbour_dir.mkdir()
        # The explicit --pid is what makes this test bite, and it is worth being
        # exact about why. Without it `register` defaults to os.getppid(), which
        # in a test is the single shell registering BOTH agents; two entries then
        # claim one pid, resolution hits the ambiguity branch, falls back to the
        # cwd, and the test fails *with the fix present*. Verified by removing it.
        #
        # (Neighbour liveness, by contrast, is irrelevant here — checked: a dead
        # neighbour discriminates just as well. `from_agent` never consults the
        # recipient-side registry, and send_message validates only `to_agent`.
        # The neighbour has to be a real registered id, not a live session.)
        _, out = _run(
            capsys,
            ["register", "--working-dir", str(neighbour_dir), "--pid", str(os.getpid())],
        )
        neighbour: str = json.loads(out)["agent_id"]
        assert neighbour != me

        _register_peer("agent-peer")
        monkeypatch.chdir(neighbour_dir)
        _run(capsys, ["send", "--to", "agent-peer", "--body", "hi"])

        attributed = [m.from_agent for m in StateStore().recv_messages("agent-peer")]
        assert attributed == [me]
        # The failure this replaces: the peer would reply to the neighbour, and
        # the neighbour would receive an answer to a question it never asked.
        assert neighbour not in attributed

    def test_focus_after_cd_updates_the_session_not_a_stranger(
        self, drifted: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(capsys, ["focus", "still me"])
        entry = next(
            a for a in StateStore().list_agents(include_stale=True) if a.agent_id == drifted
        )
        assert entry.focus == "still me"
        # And no second entry was invented for the directory we were standing in.
        assert len(StateStore().list_agents(include_stale=True)) == 1

    def test_status_after_cd_updates_the_session(
        self, drifted: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(capsys, ["status", "working"])
        entry = next(
            a for a in StateStore().list_agents(include_stale=True) if a.agent_id == drifted
        )
        assert entry.status == "working"

    def test_name_after_cd_updates_the_session(
        self, drifted: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _run(capsys, ["name", "renamed"])
        entry = next(
            a for a in StateStore().list_agents(include_stale=True) if a.agent_id == drifted
        )
        assert entry.name == "renamed"

    def test_drift_is_announced_on_stderr_not_stdout(
        self, drifted: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Rule 7, visibility over hiding: the agent is told, and stdout stays
        parseable for the callers that consume it."""
        cli.main(["agent-id"])
        captured = capsys.readouterr()
        assert captured.out.strip() == drifted
        assert "registered as" in captured.err
        assert drifted in captured.err


# ------------------------------------------------- `pid` is the claude pid (#29)


class TestRegisterRecordsTheClaudePid:
    """Owner ruling 2026-08-23: `pid` is the claude pid of the process whose
    liveness the entry tracks. Under a Bash tool `os.getppid()` is an ephemeral
    zsh, so every in-session write recorded a doomed pid — which hid the agent
    from `list_agents` and made #28's resolver silently fall back to the cwd."""

    def test_session_start_records_the_session_not_the_hooks_shell(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CLAUDE_PID", "770001")
        _run(capsys, ["session-start"])
        entry = StateStore().list_agents(include_stale=True)[0]
        assert entry.pid == 770001

    def test_register_records_the_session(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CLAUDE_PID", "770002")
        _, out = _run(capsys, ["register"])
        assert json.loads(out)["pid"] == 770002

    def test_auto_register_does_NOT_stamp_the_session_onto_a_foreign_id(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`_ensure_registered` is the one writer that must keep `getppid()`.

        An earlier revision of this PR switched it to `session_pid()` and this
        test asserted that — encoding the bug. Reaching that path from inside a
        session means the anchor found nothing, so the id is cwd-derived and by
        construction not ours. Stamping `$CLAUDE_PID` there attaches our
        liveness to a foreign entry permanently: it never decays, `is_stale`
        confirms it, and the resolver then sees two live entries claiming one
        pid. A doomed shell pid is right because it fails closed.
        """
        monkeypatch.setenv("CLAUDE_PID", "770003")
        _run(capsys, ["focus", "hello"])
        entry = StateStore().list_agents(include_stale=True)[0]
        assert entry.pid == os.getppid()
        assert entry.pid != 770003

    def test_a_human_at_a_terminal_still_gets_their_shell(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No CLAUDE_PID means no session: the parent shell IS the process whose
        liveness matters, so `getppid()` is the right answer, not a fallback we
        tolerate. This is the path the three auto-register tests exercise."""
        _, out = _run(capsys, ["register"])
        assert json.loads(out)["pid"] == os.getppid()


class TestRegisteringSomeoneElse:
    """The refusal branch. A live session writing a third party's entry with its
    own `$CLAUDE_PID` would fail OPEN — `is_stale` confirms it forever, because
    pid-alive and `pid_start` both hold of the registrar's real process."""

    def test_refuses_a_foreign_id_once_this_session_is_registered(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The guard is the ANCHOR: a live entry already holds our pid, so
        defaulting would put our liveness on a second, foreign entry."""
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
        _run(capsys, ["register"])
        before = len(StateStore().list_agents(include_stale=True))

        rc = cli.main(["register", "--agent-id", "agent-someone-else", "--name", "other"])

        captured = capsys.readouterr()
        assert rc == 1
        assert "refusing to register" in captured.err
        assert len(StateStore().list_agents(include_stale=True)) == before, "nothing written"

    def test_a_corrupt_session_may_repair_its_own_entry_from_anywhere(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """#29's victim must be able to fix itself, and the first predicate
        refused exactly that — naming a session that did not exist.

        Its entry holds a dead pid, so no live entry claims ours, so the anchor
        is empty. Empty is positive evidence that nothing live is being taken
        over, which makes the repair safe to allow.
        """
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
        _, out = _run(capsys, ["register", "--pid", "999999"])  # the corrupt state
        mine: str = json.loads(out)["agent_id"]
        assert StateStore().list_agents() == [], "precondition: filtered out as stale"

        elsewhere = cli_env.parent / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        rc, out = _run(capsys, ["register", "--agent-id", mine, "--working-dir", str(cli_env)])

        assert rc == 0
        assert json.loads(out)["pid"] == os.getpid()
        assert [a.agent_id for a in StateStore().list_agents()] == [mine]

    def test_a_drifted_register_cannot_mint_a_new_id_under_our_pid(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The other half the first predicate got backwards: plain `register`
        with no --agent-id from a drifted cwd was ALLOWED, minting a fresh id
        carrying our live pid."""
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
        _run(capsys, ["register"])
        elsewhere = cli_env.parent / "elsewhere2"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        rc = cli.main(["register"])

        assert rc == 1
        assert "refusing to register" in capsys.readouterr().err
        assert len(StateStore().list_agents()) == 1

    def test_allows_a_foreign_id_with_an_explicit_pid(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`--pid` is the documented escape hatch — how a repair is done."""
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
        rc, out = _run(
            capsys,
            ["register", "--agent-id", "agent-someone-else", "--name", "other", "--pid", "770004"],
        )
        assert rc == 0
        assert json.loads(out)["pid"] == 770004

    def test_registering_your_own_id_explicitly_is_not_refused(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CLAUDE_PID", "770005")
        mine = derive_agent_identity(cli_env)[0]
        rc, out = _run(capsys, ["register", "--agent-id", mine])
        assert rc == 0
        assert json.loads(out)["pid"] == 770005

    def test_a_human_registering_a_foreign_id_is_not_refused(
        self, cli_env: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """No session, so nothing to mis-stamp — the refusal must not leak into
        the manual path it was never about."""
        rc, out = _run(capsys, ["register", "--agent-id", "agent-someone-else", "--name", "other"])
        assert rc == 0
        assert json.loads(out)["pid"] == os.getppid()

    def test_cannot_overwrite_a_LIVE_third_party_entry(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The second operand. Guarding only *our* pid left this open.

        An unanchored session registering a healthy peer's id took that peer's
        entry: the peer fell back to a cwd-derived id and the registrar resolved
        as them on the anchor. Strictly worse than the doomed shell pid this
        used to write, which made the victim go stale visibly rather than
        handing their identity over.
        """
        peer_dir = cli_env.parent / "peer"
        peer_dir.mkdir()
        peer = derive_agent_identity(peer_dir)[0]
        _run(capsys, ["register", "--agent-id", peer, "--name", "peer", "--pid", str(os.getpid())])
        assert [a.agent_id for a in StateStore().list_agents()] == [peer]

        # A different session, with no entry of its own, defaults the pid.
        monkeypatch.setenv("CLAUDE_PID", "880404")
        rc = cli.main(["register", "--agent-id", peer, "--name", "peer"])

        assert rc == 1
        assert "already registered and LIVE" in capsys.readouterr().err
        assert StateStore().list_agents()[0].pid == os.getpid(), "peer's entry untouched"

    def test_a_STALE_entry_may_be_taken_over(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The live-only qualifier, which is what keeps repair working: a dead
        pid is #29's victim state, not a running agent to protect."""
        monkeypatch.setenv("CLAUDE_PID", str(os.getpid()))
        _, out = _run(capsys, ["register", "--pid", "999998"])
        mine: str = json.loads(out)["agent_id"]

        rc, out = _run(capsys, ["register", "--agent-id", mine])

        assert rc == 0
        assert json.loads(out)["pid"] == os.getpid()

    def test_refuses_to_grow_an_ambiguous_registry(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Two entries already claim our pid. Falling through to `allowed` made
        it three. `--pid` still bypasses, so the repair command is untouched."""
        mypid = str(os.getpid())
        monkeypatch.setenv("CLAUDE_PID", mypid)
        for name in ("dup-a", "dup-b"):
            d = cli_env.parent / name
            d.mkdir()
            _run(capsys, ["register", "--working-dir", str(d), "--pid", mypid])
        assert len(StateStore().list_agents()) == 2

        rc = cli.main(["register", "--agent-id", "agent-third"])

        assert rc == 1
        assert "ambiguous" in capsys.readouterr().err
        assert len(StateStore().list_agents()) == 2, "not grown"

    def test_explicit_pid_still_bypasses_every_guard(
        self, cli_env: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The escape hatch has to survive all of them, or a real repair is
        impossible from the one state that most needs one."""
        mypid = str(os.getpid())
        monkeypatch.setenv("CLAUDE_PID", mypid)
        for name in ("amb-a", "amb-b"):
            d = cli_env.parent / name
            d.mkdir()
            _run(capsys, ["register", "--working-dir", str(d), "--pid", mypid])

        rc, out = _run(capsys, ["register", "--agent-id", "agent-third", "--pid", "880505"])

        assert rc == 0
        assert json.loads(out)["pid"] == 880505
