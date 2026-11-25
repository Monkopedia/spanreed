"""The sleeper operations both the CLI and a peer's request run (``sleeper_ops``).

The CLI's own tests (``tests/integration/test_cli.py``) cover the local
behaviour through argparse. These call the functions directly, the way a
peer's request does, so they cover what argparse used to catch on its own.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from spanreed import sleeper_ops
from spanreed.sleepers import SleeperError
from spanreed.store import StateStore


@pytest.fixture
def tools(tmp_path: Path) -> str:
    """A PATH holding fake ``claude`` and ``spanreed``, and nothing else."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("claude", "spanreed"):
        exe = bin_dir / tool
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return str(bin_dir)


def create_args(cwd: str, **overrides: object) -> sleeper_ops.CreateArgs:
    fields: dict[str, object] = {
        "name": "nap",
        "cwd": cwd,
        "permission_mode": "plan",
        "model": None,
        "idle_timeout": None,
        "trust": False,
        "claude_args": [],
    }
    fields.update(overrides)
    return sleeper_ops.CreateArgs.model_validate(fields)


def test_create_refuses_an_unknown_permission_mode_without_argparse(
    store: StateStore, tmp_path: Path, tools: str
) -> None:
    """argparse's ``choices`` was the only check; a peer's request skips argparse."""
    with pytest.raises(SleeperError, match="'yolo' is not one of"):
        sleeper_ops.create(
            store, create_args(str(tmp_path), permission_mode="yolo"), print, path_env=tools
        )
    assert store.sleepers.read("nap") is None


def test_create_uses_the_default_idle_timeout_when_none_is_given(
    store: StateStore, tmp_path: Path, tools: str
) -> None:
    lines: list[str] = []
    assert sleeper_ops.create(store, create_args(str(tmp_path)), lines.append, path_env=tools) == 0
    record = store.sleepers.read("nap")
    assert record is not None
    assert record.idle_timeout_s == 900
    assert record.path_env == tools
    assert lines[0].startswith("created sleeper nap (agent-nap), ASLEEP")


def test_a_remote_create_refuses_a_relative_cwd(
    store: StateStore, tmp_path: Path, tools: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On the target a relative path would resolve against wherever the bridge started."""
    (tmp_path / "proj").mkdir()
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SleeperError, match="must be absolute or start with ~"):
        sleeper_ops.create(store, create_args("proj"), print, path_env=tools, remote=True)
    assert store.sleepers.read("nap") is None


def test_a_local_create_still_accepts_a_relative_cwd(
    store: StateStore, tmp_path: Path, tools: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "proj").mkdir()
    monkeypatch.chdir(tmp_path)
    assert sleeper_ops.create(store, create_args("proj"), print, path_env=tools) == 0


def test_a_remote_create_expands_tilde_to_this_users_home(
    store: StateStore, tmp_path: Path, tools: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    assert sleeper_ops.create(store, create_args("~/proj"), print, path_env=tools, remote=True) == 0
    record = store.sleepers.read("nap")
    assert record is not None and record.cwd == str((home / "proj").resolve())


def test_a_remote_create_names_remote_access_when_its_path_finds_no_claude(
    store: StateStore, tmp_path: Path
) -> None:
    with pytest.raises(SleeperError, match="recorded by `spanreed sleeper remote-access on`"):
        sleeper_ops.create(
            store, create_args(str(tmp_path)), print, path_env=str(tmp_path), remote=True
        )


@pytest.mark.parametrize("op", [sleeper_ops.wake, sleeper_ops.sleep, sleeper_ops.retire])
def test_wake_sleep_and_retire_refuse_a_name_that_is_not_a_legal_id(
    store: StateStore, op: Callable[[StateStore, str, sleeper_ops.Out], int]
) -> None:
    """The name becomes a file name (``sleepers/<name>.json``); a peer can send any string."""
    with pytest.raises(SleeperError, match="is not legal"):
        op(store, "../escape", print)


def test_list_returns_the_records_as_json(store: StateStore, tmp_path: Path, tools: str) -> None:
    sleeper_ops.create(store, create_args(str(tmp_path)), print, path_env=tools)
    records = sleeper_ops.list_records(store)
    assert [r["name"] for r in records] == ["nap"]
    lines: list[str] = []
    assert sleeper_ops.list_sleepers(store, lines.append) == 0
    assert lines[0] == "nap  ASLEEP  (agent-nap)"
