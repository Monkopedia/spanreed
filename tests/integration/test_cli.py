"""Tests for the spanreed CLI.

Calls ``cli.main(argv)`` with stdout captured. State root is per-test via the
``SPANREED_STATE_ROOT`` env var (so each test gets a pristine bus).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from spanreed import cli
from spanreed.store import StateStore


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, state_root: Path, tmp_path: Path) -> Iterator[Path]:
    """Bind CLI to a per-test state root and a per-test cwd for identity derivation."""
    monkeypatch.setenv("SPANREED_STATE_ROOT", str(state_root))
    monkeypatch.delenv("SPANREED_AGENT_NAME", raising=False)
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
