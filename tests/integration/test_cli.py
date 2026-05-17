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


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, state_root: Path, tmp_path: Path) -> Iterator[Path]:
    """Bind CLI to a per-test state root and a per-test cwd for identity derivation."""
    monkeypatch.setenv("SPANREED_STATE_ROOT", str(state_root))
    monkeypatch.delenv("SPANREED_AGENT_NAME", raising=False)
    session_cwd = tmp_path / "session-cwd"
    session_cwd.mkdir()
    monkeypatch.chdir(session_cwd)
    yield session_cwd


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
        _, id_out = _run(capsys, ["agent-id"])
        my_id = id_out.strip()
        _run(capsys, ["send", "--to", "agent-B", "--body", "hi"])
        _, recv_out = _run(capsys, ["recv", "agent-B"])
        msgs = json.loads(recv_out)
        assert msgs[0]["from_agent"] == my_id

    def test_send_with_in_reply_to(self, cli_env: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
