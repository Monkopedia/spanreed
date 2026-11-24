"""The published console scripts, run by name as the plugin runs them.

The plugin reaches this package only through two names: ``.mcp.json`` starts
``spanreed-mcp``, and the SessionStart hook and Monitor call ``spanreed``. Every
other test calls ``cli.main`` or the server in-process (or via ``python -m``), so
a typo in ``[project.scripts]`` or a broken entry function would pass them all
and still break every session at plugin load (#21).

The scripts are resolved from the scripts directory of the interpreter running
the tests, i.e. this checkout's venv. A ``spanreed`` on the developer's PATH is
the released tool, not this checkout, and passing against it would prove
nothing.
"""

from __future__ import annotations

import os
import subprocess
import sysconfig
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import pytest

from spanreed.identity import derive_agent_identity


def _script(name: str) -> Path:
    try:
        distribution("spanreed-bus")
    except PackageNotFoundError:
        pytest.skip("spanreed-bus is not installed here, so no console scripts were created")
    # Installed but missing is the regression itself (a renamed script), so it
    # fails rather than skips.
    path = Path(sysconfig.get_path("scripts")) / name
    assert path.is_file(), f"console script {name!r} was not installed in {path.parent}"
    return path


def _env(state_root: Path) -> dict[str, str]:
    # Built from scratch so the developer's session (CLAUDE_PID,
    # SPANREED_AGENT_NAME) cannot change which identity the script resolves.
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "SPANREED_STATE_ROOT": str(state_root),
    }


@pytest.fixture
def session_cwd(tmp_path: Path) -> Path:
    cwd = tmp_path / "session-cwd"
    cwd.mkdir()
    return cwd


def test_spanreed_runs_the_cli(
    state_root: Path, session_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = subprocess.run(
        [_script("spanreed"), "agent-id"],
        env=_env(state_root),
        cwd=session_cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    monkeypatch.delenv("SPANREED_AGENT_NAME", raising=False)
    assert result.stdout.strip() == derive_agent_identity(session_cwd)[0]


def test_spanreed_mcp_runs_the_server_and_exits_clean_on_eof(
    state_root: Path, session_cwd: Path
) -> None:
    result = subprocess.run(
        [_script("spanreed-mcp")],
        env=_env(state_root),
        cwd=session_cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    # main() logs both ends of its run; seeing them proves the name resolved to
    # main() itself and not to something else that happens to exit 0.
    log = (state_root / "logs" / "spanreed-mcp.log").read_text()
    assert "event=startup" in log
    assert "event=shutdown reason=clean" in log
