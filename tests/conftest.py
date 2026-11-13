"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from spanreed import sleeper_runner
from spanreed.store import StateStore


@pytest.fixture
def state_root(tmp_path: Path) -> Path:
    """A pristine state root under pytest's tmp dir."""
    root = tmp_path / "spanreed-state"
    root.mkdir()
    return root


@pytest.fixture
def store(state_root: Path) -> Iterator[StateStore]:
    """A StateStore bound to a pristine per-test directory."""
    yield StateStore(root=state_root)


@pytest.fixture(autouse=True)
def no_real_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test launches a real tmux session by accident.

    Mail to a sleeper wakes it, and a wake runs ``tmux new-session``. A test that
    sends to a sleeper would otherwise start a session on the developer's tmux
    server. Tests of waking replace this with their own fake.
    """

    def refuse(*_args: object, **_kwargs: object) -> int:
        raise sleeper_runner.LaunchError("tests do not launch tmux")

    monkeypatch.setattr(sleeper_runner, "_launch_runner", refuse)
