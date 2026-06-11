"""Smoke tests: prove the package imports and basic types serialize."""

from __future__ import annotations

import json
import tomllib
from datetime import datetime
from importlib.metadata import version
from pathlib import Path

from spanreed import __version__
from spanreed.protocol import Agent, Message

_REPO_ROOT = Path(__file__).resolve().parents[2]


def test_version_matches_package_metadata() -> None:
    # __version__ is derived from installed metadata, so it can't drift from the
    # packaged version the way a hardcoded string did.
    assert __version__ == version("spanreed-bus")


def test_plugin_version_matches_pyproject() -> None:
    # The plugin and the PyPI package ship in lockstep — the plugin's MCP server
    # runs the installed `spanreed` package. Claude Code's `/plugin update` keys
    # off plugin.json's version, NOT pyproject's, so if these drift the fleet
    # silently never updates (it once sat at 0.0.1 while the package was 0.0.5).
    # Pin them together so a release that bumps one must bump the other.
    pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    pkg_version = pyproject["project"]["version"]
    plugin = json.loads(
        (_REPO_ROOT / "plugins" / "spanreed" / ".claude-plugin" / "plugin.json").read_text()
    )
    assert plugin["version"] == pkg_version


def test_agent_serializes() -> None:
    agent = Agent(
        agent_id="agent-abc",
        name="alice",
        working_dir="/tmp/foo",
        pid=12345,
        last_seen=datetime(2026, 5, 17, 12, 0, 0),
    )
    payload = json.loads(agent.model_dump_json())
    assert payload["agent_id"] == "agent-abc"
    assert payload["name"] == "alice"


def test_message_serializes() -> None:
    msg = Message(
        msg_id="msg-1",
        from_agent="agent-abc",
        to_agent="agent-def",
        body="hello",
        ts=datetime(2026, 5, 17, 12, 0, 0),
    )
    payload = json.loads(msg.model_dump_json())
    assert payload["from_agent"] == "agent-abc"
    assert payload["to_agent"] == "agent-def"
    assert payload["body"] == "hello"
    assert payload["in_reply_to"] is None
