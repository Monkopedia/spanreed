"""The `pid` invariant table in ``docs/protocol.md`` ("What `pid` means"), pinned.

The invariant is that a row written for this session's own id, from inside a
session, records ``$CLAUDE_PID``. Each other writer is an exemption, and the doc
lists each one as a row of the same table. This file has one case per writer and
anchor state. A behaviour change moves a case here and a row there, and both
belong in the same commit.

Why every writer is run under every anchor state, even writers that never read
the anchor: an exemption that fails only when an anchor is set is the kind of
regression the anchor-free tests let through. A stub writer tested only with
``$CLAUDE_PID`` unset passes whether it records ``getppid()`` or the anchor,
because the two are equal then (#38). The anchored columns ("770001" and "1")
are what distinguish them.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spanreed import cli, mcp_server
from spanreed.protocol import Agent
from spanreed.sleepers import SleeperRecord
from spanreed.store import StateStore
from tests.unit.test_codex_worker import MakeWorker

PARENT = os.getppid()
"""What ``getppid()`` returns for the CLI writers, which run in this process."""

# The anchor states. "Anchored" is a positive decimal integer; everything else is
# treated as unset (#94). Pid 1 is anchored and recorded, but liveness then reads
# the row as stale ("Liveness / staleness").
ANCHORS = {
    "unset": None,
    "anchored": "770001",
    "zero": "0",
    "negative": "-1",
    "non-decimal-digit": "²",
    "pid-1": "1",
}

# What a writer that follows the anchor records, per anchor state.
FOLLOWS_ANCHOR = {
    "unset": PARENT,
    "anchored": 770001,
    "zero": PARENT,
    "negative": PARENT,
    "non-decimal-digit": PARENT,
    "pid-1": 1,
}


def _constant(pid: int) -> dict[str, int]:
    return dict.fromkeys(ANCHORS, pid)


def _cli(*argv: str) -> Callable[[pytest.FixtureRequest], str]:
    def write(_request: pytest.FixtureRequest) -> str:
        assert cli.main(list(argv)) == 0
        return ""

    return write


def _with_override(writer: Callable[[pytest.FixtureRequest], str]) -> Callable[..., str]:
    def write(request: pytest.FixtureRequest) -> str:
        request.getfixturevalue("monkeypatch").setenv("SPANREED_AGENT_NAME", "override")
        return writer(request)

    return write


def _mirror(_request: pytest.FixtureRequest) -> str:
    remote = Agent(
        agent_id="agent-remote",
        name="remote",
        working_dir="/r",
        pid=12,
        last_seen=datetime.now(UTC),
    )
    StateStore().sync_remote_agents("peerhost", [remote], owner_pid=4343, owner_pid_start=None)
    return "agent-remote@peerhost"


def _sleeper(_request: pytest.FixtureRequest) -> str:
    StateStore().create_sleeper(
        SleeperRecord(
            name="nap",
            agent_id="agent-nap",
            cwd="/tmp",
            session_id="00000000-0000-4000-8000-000000000000",
            permission_mode="acceptEdits",
            claude_path="/usr/bin/claude",
            spanreed_path="/usr/bin/spanreed",
            path_env="/usr/bin",
            created_at=datetime.now(UTC),
        )
    )
    return "agent-nap"


def _codex_worker(request: pytest.FixtureRequest) -> str:
    make_worker: MakeWorker = request.getfixturevalue("make_worker")
    _stub, worker = make_worker()
    return worker.config.agent_id


def _mcp_register(_request: pytest.FixtureRequest) -> str:
    mcp_server.register_agent(name="m", working_dir="/m", pid=4444, agent_id="agent-mcp")
    return "agent-mcp"


# One entry per row of the table in docs/protocol.md, in the same order:
# (row, writer, the pid it records under each anchor state). A writer returning
# "" means "the only row in the registry".
TABLE: list[tuple[str, Callable[[pytest.FixtureRequest], str], dict[str, int]]] = [
    ("session-start", _cli("session-start"), FOLLOWS_ANCHOR),
    ("session-start, override set", _with_override(_cli("session-start")), FOLLOWS_ANCHOR),
    ("register, pid defaulted", _cli("register"), FOLLOWS_ANCHOR),
    (
        "register --agent-id <foreign>, pid defaulted",
        _cli("register", "--agent-id", "agent-foreign"),
        FOLLOWS_ANCHOR,
    ),
    ("register --pid", _cli("register", "--pid", "4242"), _constant(4242)),
    ("_ensure_registered, cwd-derived id", _cli("focus", "f"), _constant(PARENT)),
    ("_ensure_registered, override set", _with_override(_cli("focus", "f")), _constant(PARENT)),
    ("mirrored @peer row", _mirror, _constant(4343)),
    ("sleeper row, asleep", _sleeper, _constant(0)),
    ("codex worker", _codex_worker, _constant(os.getpid())),
    # OPEN, #39: whether the MCP tool should anchor on getppid() at server start
    # or require an explicit pid is undecided. This pins today's behaviour (the
    # caller's pid, unchecked) so that deciding #39 has to change this case.
    ("MCP register_agent (open, #39)", _mcp_register, _constant(4444)),
]


@pytest.mark.parametrize("anchor", list(ANCHORS))
@pytest.mark.parametrize(("row", "writer", "expected"), TABLE, ids=[r for r, _, _ in TABLE])
def test_pid_recorded_by_every_writer_under_every_anchor_state(
    row: str,
    writer: Callable[[pytest.FixtureRequest], str],
    expected: dict[str, int],
    anchor: str,
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    state_root: Path,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("SPANREED_STATE_ROOT", str(state_root))
    monkeypatch.delenv("SPANREED_AGENT_NAME", raising=False)
    value = ANCHORS[anchor]
    if value is None:
        monkeypatch.delenv("CLAUDE_PID", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_PID", value)
    cwd = tmp_path / "session-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)

    agent_id = writer(request)

    rows = StateStore().list_agents(include_stale=True)
    entry = next(a for a in rows if a.agent_id == agent_id) if agent_id else rows[0]
    if not agent_id:
        assert len(rows) == 1, f"{row}: expected exactly one row, got {rows}"
    assert entry.pid == expected[anchor], f"{row} under anchor {anchor}"
