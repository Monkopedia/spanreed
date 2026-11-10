"""Bus state is owner-only (#18), set explicitly rather than by umask.

Every test runs under a deliberately permissive umask (000). Under the usual
022, a default-mode `mkdir` gives 0755 and a test asserting "not world-
writable" would pass without proving anything about the modes this code sets.
Under 000 an unprotected create comes out 0777/0666, so only an explicit mode
produces 0700/0600.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from spanreed import _fsmode
from spanreed.protocol import PeerLink
from spanreed.store import StateStore


def _mode(p: Path) -> int:
    return stat.S_IMODE(os.lstat(p).st_mode)


@pytest.fixture
def open_umask() -> Iterator[None]:
    old = os.umask(0o000)
    try:
        yield
    finally:
        os.umask(old)


def _loose_paths(root: Path) -> list[str]:
    loose: list[str] = []
    for dirpath, _dirs, files in os.walk(root):
        d = Path(dirpath)
        if _mode(d) != 0o700:
            loose.append(f"{d.relative_to(root.parent)} {oct(_mode(d))}")
        for f in files:
            p = d / f
            if not p.is_symlink() and _mode(p) != 0o600:
                loose.append(f"{p.relative_to(root.parent)} {oct(_mode(p))}")
    return loose


class TestNewStateIsPrivate:
    def test_every_store_write_path_creates_owner_only(
        self, tmp_path: Path, open_umask: None
    ) -> None:
        # Exercise every creation site in store.py: dirs, registry (+tmp, lock),
        # inbox (send and append), cursor, config, activity log, peer link.
        store = StateStore(root=tmp_path / "state")
        store.register_agent(name="a", working_dir="/a", pid=os.getpid(), agent_id="agent-a")
        store.register_agent(name="b", working_dir="/b", pid=os.getpid(), agent_id="agent-b")
        store.set_activity_log(True)
        store.set_focus("agent-a", "working on it")
        msg = store.send_message(from_agent="agent-a", to_agent="agent-b", body="hi")
        store.set_cursor(".bridge.agent-b@adolin", msg.msg_id)
        store.write_peer_link(
            PeerLink.model_validate(
                {
                    "host": "adolin",
                    "role": "connect",
                    "bridge_pid": os.getpid(),
                    "bridge_pid_start": None,
                    "attached_at": "2026-01-01T00:00:00Z",
                }
            )
        )
        created = {p.name for p in (tmp_path / "state").rglob("*")}
        for expected in (
            "inboxes",
            "cursors",
            "peers",
            "registry.json",
            "registry.lock",
            "config.json",
            "activity-log.jsonl",
            "adolin.json",
        ):
            assert expected in created, (
                f"{expected} was never created; the test covers less than it claims"
            )
        assert _loose_paths(tmp_path / "state") == []

    def test_the_umask_is_left_alone(self, tmp_path: Path) -> None:
        # The modes are explicit, not a process-wide umask change: the Codex
        # worker spawns `codex`, which would inherit it and write the user's
        # project files 0600.
        def current_umask() -> int:
            value = os.umask(0)  # the only way to read it is to set it
            os.umask(value)
            return value

        before = current_umask()
        store = StateStore(root=tmp_path / "state")
        store.register_agent(name="b", working_dir="/b", pid=os.getpid(), agent_id="agent-b")
        store.send_message(from_agent="agent-b", to_agent="agent-b", body="x")
        _fsmode.tighten_tree(tmp_path / "state")
        assert current_umask() == before


class TestTightenTree:
    def test_existing_loose_state_is_tightened_and_counted(
        self, tmp_path: Path, open_umask: None
    ) -> None:
        root = tmp_path / "state"
        (root / "inboxes").mkdir(parents=True, mode=0o755)
        root.chmod(0o755)
        (root / "registry.json").write_text("{}")
        (root / "inboxes" / "agent-old.jsonl").write_text("history\n")
        os.chmod(root / "registry.json", 0o644)
        os.chmod(root / "inboxes" / "agent-old.jsonl", 0o644)

        assert _fsmode.tighten_tree(root) == 4  # root, inboxes/, two files
        assert _loose_paths(root) == []
        assert _fsmode.tighten_tree(root) == 0, "a second pass must be a no-op"

    def test_symlinks_are_not_followed(self, tmp_path: Path) -> None:
        # A link in the state root must not get its TARGET chmodded.
        outside = tmp_path / "outside.txt"
        outside.write_text("not ours")
        outside.chmod(0o644)
        root = tmp_path / "state"
        root.mkdir()
        (root / "link").symlink_to(outside)
        _fsmode.tighten_tree(root)
        assert _mode(outside) == 0o644

    def test_a_symlinked_directory_is_not_descended_into(self, tmp_path: Path) -> None:
        # Safety here rests on os.walk's followlinks=False; the file-symlink test
        # above cannot see it, so `os.walk(root, followlinks=True)` passed the
        # whole suite (#75 review). A symlinked dir whose target holds a 0644
        # file must leave that target, and the file in it, untouched.
        target = tmp_path / "elsewhere"
        target.mkdir()
        target.chmod(0o755)
        (target / "theirs.txt").write_text("not ours")
        (target / "theirs.txt").chmod(0o644)
        root = tmp_path / "state"
        root.mkdir()
        (root / "linked-dir").symlink_to(target, target_is_directory=True)
        _fsmode.tighten_tree(root)
        assert _mode(target) == 0o755
        assert _mode(target / "theirs.txt") == 0o644

    def test_a_missing_root_is_a_no_op(self, tmp_path: Path) -> None:
        assert _fsmode.tighten_tree(tmp_path / "nope") == 0


class TestOtherWriters:
    def test_mcp_log_disposition_policy_and_worker_log_are_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, open_umask: None
    ) -> None:
        from spanreed import cli, mcp_server
        from spanreed.codex_worker import WorkerLog

        root = tmp_path / "state"
        monkeypatch.setenv("SPANREED_STATE_ROOT", str(root))
        mcp_server._log("probe")  # pyright: ignore[reportPrivateUsage]
        cli._write_disposition_policy()  # pyright: ignore[reportPrivateUsage]
        WorkerLog(root / "codex" / "w.log", stream=None).write("line")
        for p in (
            root / "logs" / "spanreed-mcp.log",
            root / "disposition-policy.md",
            root / "codex" / "w.log",
        ):
            assert p.exists(), p
        assert _loose_paths(root) == []

    def test_a_worker_log_recreated_after_deletion_is_still_private(
        self, tmp_path: Path, open_umask: None
    ) -> None:
        # The log is created private at construction, so later appends only
        # CREATE the file if something removed it in between (log rotation, a
        # tidy-up). That recreation is the case the append's mode exists for.
        from spanreed.codex_worker import WorkerLog

        log = WorkerLog(tmp_path / "codex" / "w.log", stream=None)
        log.path.unlink()
        log.write("after rotation")
        assert _mode(log.path) == 0o600


class TestExistingFilesWrittenThrough:
    def test_a_stale_loose_tmp_cannot_publish_a_loose_registry(self, tmp_path: Path) -> None:
        # O_CREAT's mode does nothing for a file that already exists, so a
        # 0644 `registry.json.tmp` left by an older version would survive the
        # truncate-and-replace and become a 0644 registry.json.
        root = tmp_path / "state"
        store = StateStore(root=root)
        stale = root / "registry.json.tmp"
        stale.write_text("old")
        stale.chmod(0o644)
        store.register_agent(name="a", working_dir="/a", pid=os.getpid(), agent_id="agent-a")
        assert _mode(root / "registry.json") == 0o600
