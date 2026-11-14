"""Tests for reading and writing Claude Code's folder trust (``sleeper create --trust``)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from spanreed import claude_trust
from spanreed.claude_trust import TrustError, claude_state_file, folder_is_trusted, trust_folder


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / ".claude.json"
    path.write_text(
        json.dumps(
            {
                "numStartups": 3,
                "projects": {
                    "/home/u/git": {"hasTrustDialogAccepted": True, "allowedTools": ["x"]},
                    "/home/u/other": {"hasTrustDialogAccepted": False},
                },
            }
        )
    )
    path.chmod(0o600)
    return path


class TestFolderIsTrusted:
    def test_a_trusted_folder(self, state: Path) -> None:
        assert folder_is_trusted(Path("/home/u/git"), state) is True

    def test_trust_is_inherited_from_a_parent(self, state: Path) -> None:
        """Measured (findings Test 14): a folder under a trusted one opens with no dialog."""
        assert folder_is_trusted(Path("/home/u/git/repo/sub"), state) is True

    def test_an_explicit_no_and_an_unknown_folder(self, state: Path) -> None:
        assert folder_is_trusted(Path("/home/u/other"), state) is False
        assert folder_is_trusted(Path("/srv/x"), state) is False

    def test_no_state_file_means_nothing_is_trusted(self, tmp_path: Path) -> None:
        assert folder_is_trusted(Path("/x"), tmp_path / "missing.json") is False

    def test_an_unreadable_state_file_is_unknown(self, tmp_path: Path) -> None:
        bad = tmp_path / ".claude.json"
        bad.write_text("{not json")
        assert folder_is_trusted(Path("/x"), bad) is None
        bad.write_text(json.dumps({"projects": []}))
        assert folder_is_trusted(Path("/x"), bad) is None

    def test_the_state_file_follows_claude_config_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
        assert claude_state_file() == tmp_path / "cfg" / ".claude.json"
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        assert claude_state_file() == Path.home() / ".claude.json"


class TestTrustFolder:
    def test_sets_the_key_and_keeps_everything_else(self, state: Path) -> None:
        trust_folder(Path("/srv/new"), state)
        data = json.loads(state.read_text())
        assert data["projects"]["/srv/new"] == {"hasTrustDialogAccepted": True}
        assert data["numStartups"] == 3
        assert data["projects"]["/home/u/git"]["allowedTools"] == ["x"]
        assert folder_is_trusted(Path("/srv/new"), state) is True

    def test_flips_an_existing_no(self, state: Path) -> None:
        trust_folder(Path("/home/u/other"), state)
        assert folder_is_trusted(Path("/home/u/other"), state) is True

    def test_keeps_the_files_mode(self, state: Path) -> None:
        trust_folder(Path("/srv/new"), state)
        assert stat.S_IMODE(state.stat().st_mode) == 0o600
        assert [p.name for p in state.parent.iterdir()] == [".claude.json"], "no temp left"

    def test_a_concurrent_write_is_not_undone(
        self, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A Claude session rewriting the file mid-write: start over from its version."""
        real_dump = claude_trust.json.dump
        calls = {"n": 0}

        def dump_and_race(obj: object, f: object, **kw: object) -> None:
            calls["n"] += 1
            if calls["n"] == 1:
                data = json.loads(state.read_text())
                data["numStartups"] = 4  # the other session's write
                state.write_text(json.dumps(data) + "\n")
            real_dump(obj, f, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(claude_trust.json, "dump", dump_and_race)
        trust_folder(Path("/srv/new"), state)
        data = json.loads(state.read_text())
        assert data["numStartups"] == 4, "the session's write survived"
        assert data["projects"]["/srv/new"]["hasTrustDialogAccepted"] is True
        assert calls["n"] == 2

    def test_a_missing_state_file_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(TrustError, match="has not been run as this user"):
            trust_folder(Path("/x"), tmp_path / ".claude.json")

    def test_an_unexpected_shape_is_refused_and_left_alone(self, tmp_path: Path) -> None:
        path = tmp_path / ".claude.json"
        path.write_text(json.dumps({"projects": "nope"}))
        with pytest.raises(TrustError, match="no `projects` table"):
            trust_folder(Path("/x"), path)
        assert json.loads(path.read_text()) == {"projects": "nope"}

    def test_a_file_that_never_settles_gives_up(
        self, state: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real_dump = claude_trust.json.dump

        def always_race(obj: object, f: object, **kw: object) -> None:
            os.utime(state, ns=(state.stat().st_atime_ns, state.stat().st_mtime_ns + 1_000_000))
            real_dump(obj, f, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(claude_trust.json, "dump", always_race)
        with pytest.raises(TrustError, match="kept changing"):
            trust_folder(Path("/srv/new"), state)
        assert "/srv/new" not in json.loads(state.read_text())["projects"]
