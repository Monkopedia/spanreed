"""The MCP server over its real stdio transport: what an agent actually receives.

``test_mcp_server.py`` calls the tool functions directly, which cannot see what
the server does to a tool's error on the way out. Since mcp 2.1, an exception
that is not a ``ToolError`` reaches the client as a bare "Error executing tool
<name>", which drops the guidance spanreed's errors exist to give (review of
#103). These run ``spanreed-mcp`` as a subprocess and read the client's view.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.types import TextContent


async def _call(
    state_root: Path, cwd: Path, tool: str, arguments: dict[str, object]
) -> tuple[bool, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "SPANREED_STATE_ROOT": str(state_root),
    }
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "spanreed.mcp_server"], env=env, cwd=str(cwd)
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        result = await session.call_tool(tool, arguments)
    text = "".join(c.text for c in result.content if isinstance(c, TextContent))
    return bool(result.is_error), text


@pytest.fixture
def session_cwd(tmp_path: Path) -> Path:
    cwd = tmp_path / "session-cwd"
    cwd.mkdir()
    return cwd


async def test_an_unknown_recipient_keeps_its_guidance(state_root: Path, session_cwd: Path) -> None:
    is_error, text = await _call(
        state_root,
        session_cwd,
        "send_message",
        {"from_agent": "a", "to_agent": "nobody-here", "body": "hi"},
    )
    assert is_error
    assert "is not a registered agent_id" in text
    assert "list_agents" in text, "the next step the agent should take"


async def test_request_sleep_outside_a_sleeper_says_why(
    state_root: Path, session_cwd: Path
) -> None:
    is_error, text = await _call(state_root, session_cwd, "request_sleep", {})
    assert is_error
    assert "only for sleepers" in text


async def test_a_successful_call_is_not_an_error(state_root: Path, session_cwd: Path) -> None:
    is_error, text = await _call(state_root, session_cwd, "list_agents", {})
    assert not is_error, text
