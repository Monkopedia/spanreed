"""Smoke tests: prove the package imports and basic types serialize."""

from __future__ import annotations

import json
from datetime import datetime

from spanreed import __version__
from spanreed.protocol import Agent, Message


def test_version_is_set() -> None:
    assert __version__


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
