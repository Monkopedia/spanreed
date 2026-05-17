"""Integration tests for the MCP server tools.

We call the tool functions directly (the @mcp_app.tool() decorator leaves them
callable) instead of going through the stdio transport. That tests the tool
wiring + StateStore integration without re-testing the MCP library itself.
State root is per-test via the ``SPANREED_STATE_ROOT`` env var.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest

from spanreed.mcp_server import (
    deregister_agent,
    list_agents,
    recv_messages,
    register_agent,
    send_message,
    wait_for_reply,
)


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch, state_root: Path) -> Iterator[None]:
    """Bind the MCP server tools to a per-test state root."""
    monkeypatch.setenv("SPANREED_STATE_ROOT", str(state_root))
    yield


def test_register_then_list(mcp_env: None) -> None:
    agent = register_agent(name="alice", working_dir="/tmp/x", pid=os.getpid())
    assert agent["name"] == "alice"
    agents = list_agents()
    assert any(a["agent_id"] == agent["agent_id"] for a in agents)


def test_register_with_explicit_id_upserts(mcp_env: None) -> None:
    first = register_agent(
        name="alice-old",
        working_dir="/tmp/x",
        pid=os.getpid(),
        agent_id="agent-fixed",
    )
    second = register_agent(
        name="alice-new",
        working_dir="/tmp/y",
        pid=os.getpid(),
        agent_id="agent-fixed",
    )
    assert second["agent_id"] == first["agent_id"]
    matching = [a for a in list_agents() if a["agent_id"] == "agent-fixed"]
    assert len(matching) == 1
    assert matching[0]["name"] == "alice-new"


def test_deregister_returns_ok(mcp_env: None) -> None:
    agent = register_agent(name="alice", working_dir="/tmp/x", pid=os.getpid())
    result = deregister_agent(agent_id=str(agent["agent_id"]))
    assert result == {"ok": True}
    assert not any(a["agent_id"] == agent["agent_id"] for a in list_agents())


def test_send_and_recv_round_trip(mcp_env: None) -> None:
    msg = send_message(from_agent="A", to_agent="B", body="hello")
    assert msg["body"] == "hello"
    delivered = recv_messages(agent_id="B")
    assert len(delivered) == 1
    assert delivered[0]["msg_id"] == msg["msg_id"]


def test_recv_with_cursor(mcp_env: None) -> None:
    m1 = send_message(from_agent="A", to_agent="B", body="one")
    m2 = send_message(from_agent="A", to_agent="B", body="two")
    after = recv_messages(agent_id="B", since_msg_id=str(m1["msg_id"]))
    assert len(after) == 1
    assert after[0]["msg_id"] == m2["msg_id"]


def test_send_with_in_reply_to(mcp_env: None) -> None:
    m1 = send_message(from_agent="A", to_agent="B", body="ping")
    m2 = send_message(
        from_agent="B",
        to_agent="A",
        body="pong",
        in_reply_to=str(m1["msg_id"]),
    )
    assert m2["in_reply_to"] == m1["msg_id"]


async def test_wait_for_reply_returns_reply(mcp_env: None) -> None:
    m1 = send_message(from_agent="A", to_agent="B", body="ping")

    async def post_reply() -> None:
        await anyio.sleep(0.1)
        send_message(
            from_agent="B",
            to_agent="A",
            body="pong",
            in_reply_to=str(m1["msg_id"]),
        )

    reply: dict[str, object] | None = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(post_reply)
        reply = await wait_for_reply(agent_id="A", in_reply_to=str(m1["msg_id"]), timeout_s=2.0)

    assert reply is not None
    assert reply["body"] == "pong"
    assert reply["in_reply_to"] == m1["msg_id"]


async def test_wait_for_reply_times_out(mcp_env: None) -> None:
    m1 = send_message(from_agent="A", to_agent="B", body="ping")
    reply = await wait_for_reply(agent_id="A", in_reply_to=str(m1["msg_id"]), timeout_s=0.2)
    assert reply is None
