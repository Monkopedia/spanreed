"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

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
