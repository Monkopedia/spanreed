"""Tests for ``spanreed codex --doctor``.

The doctor exists because the machine that runs a Codex worker is one its
author cannot debug on: each iteration costs a release, and the only evidence
that comes back is a pasted file. So the properties tested here are mostly
*about the reporting* -- that a step which did not run is never shown as a pass,
that an accepted turn is never reported as a completed one, and that nothing
secret reaches the log -- rather than about the protocol, which
``test_codex_client.py`` already covers.

The stub server is imported from ``test_codex_client``; a second one would drift
from the first and then prove something about itself.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any

import pytest

from spanreed.codex_doctor import MARKER, Report, Step, redacted_auth, run_doctor
from tests.unit.test_codex_client import StubServer


class TestVerdictReporting:
    """A step's verdict must never overstate what happened."""

    def test_a_fresh_step_has_not_passed(self) -> None:
        # The default matters: steps are created up front so they all appear in
        # the report, and one that never executed must say so. The spike printed
        # SKIP and PASS for steps that had not run and the distinction was lost.
        s = Step(1, "q")
        assert s.verdict == "DID NOT RUN"
        assert s.verdict != "PASS"

    def test_did_not_run_is_not_counted_as_success(self) -> None:
        rep = Report(out=io.StringIO())
        rep.steps.append(Step(1, "ran", verdict="PASS", detail="d"))
        rep.steps.append(Step(2, "never ran"))
        rendered = _render(rep)
        assert "DID NOT RUN" in rendered
        assert "No failures, but some steps DID NOT RUN. That is not a pass." in rendered

    def test_a_failure_says_later_steps_may_be_untested(self) -> None:
        rep = Report(out=io.StringIO())
        rep.steps.append(Step(1, "broke", verdict="FAIL", detail="d"))
        rendered = _render(rep)
        assert "step(s) FAILED" in rendered
        # A long specific phrase, not a substring that could match by luck --
        # and one that does not span the line break, since collapsing whitespace
        # by hand got this wrong once already.
        assert "may have been skipped rather than tested" in rendered


class TestAuthRedaction:
    """auth.json holds live credentials. The doctor reports shapes only."""

    def test_token_values_never_appear(self, tmp_path: Path) -> None:
        secret = "sk-DOCTOR-MUST-NOT-PRINT-THIS"
        (tmp_path / "auth.json").write_text(
            json.dumps(
                {
                    "auth_mode": "chatgpt",
                    "OPENAI_API_KEY": secret + "-apikey",
                    "tokens": {"access_token": secret, "refresh_token": secret + "-refresh"},
                }
            )
        )
        rendered = json.dumps(redacted_auth(tmp_path))
        assert secret not in rendered
        # A prefix check too: a truncated token is still a leaked token.
        assert "sk-DOCTOR" not in rendered
        # But the diagnostic content must survive redaction, or the probe is
        # useless and someone will "fix" it by printing more.
        assert "chatgpt" in rendered
        assert "access_token" in rendered  # key NAME is not a secret
        assert "auth_mode" in rendered

    def test_missing_file_is_reported_not_crashed(self, tmp_path: Path) -> None:
        out = redacted_auth(tmp_path)
        assert out["exists"] is False

    def test_unparseable_file_is_reported(self, tmp_path: Path) -> None:
        (tmp_path / "auth.json").write_text("{not json")
        out = redacted_auth(tmp_path)
        assert out["exists"] is True
        assert "unreadable" in out


class TestAgainstAStubServer:
    """End to end against the stub, for the paths that do not need a real model."""

    def test_no_codex_on_path_fails_step_zero(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        buf = io.StringIO()
        log = tmp_path / "d.log"
        rc = run_doctor(cwd=tmp_path, log_path=log, out=buf)
        assert rc == 1
        text = buf.getvalue()
        assert "NOT FOUND" in text
        # and the later steps must be visibly untested, not silently absent
        assert "DID NOT RUN" in text
        assert log.exists(), "the log file must be written even when step 0 fails"

    def test_log_file_is_self_contained(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        log = tmp_path / "d.log"
        run_doctor(cwd=tmp_path, log_path=log, out=io.StringIO())
        text = log.read_text()
        # Everything a reader needs is in the file, not only on the terminal.
        assert "Key facts" in text
        assert "Result" in text
        assert "FULL LOG" in text

    def test_danger_mode_warns_unmissably(self, tmp_path: Path, monkeypatch: Any) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        buf = io.StringIO()
        run_doctor(cwd=tmp_path, mode="danger", log_path=tmp_path / "d.log", out=buf)
        text = buf.getvalue()
        assert "MODE=danger" in text
        assert "NO SANDBOX" in text
        assert "!!" in text

    def test_marker_is_specific_enough_not_to_occur_by_chance(self) -> None:
        # A marker that could appear in ordinary model output would make step 3
        # pass on a reply that never contained it.
        assert MARKER.isupper()
        assert len(MARKER) > 12
        assert "_" in MARKER


def _render(rep: Report) -> str:
    """Run the report's own finish path and return what it printed."""
    from spanreed.codex_doctor import finish

    buf = io.StringIO()
    rep.out = buf
    fh = io.StringIO()
    finish(rep, fh, Path("/tmp/x.log"))
    return buf.getvalue()


def test_stub_import_is_the_shared_one() -> None:
    """Guard against a second stub appearing. See the module docstring."""
    assert StubServer.__module__ == "tests.unit.test_codex_client"


class TestStartupPreconditions:
    """The doctor and the worker must agree about whether a worker can start.

    A worker refuses to start when it cannot write its approval log. Before
    this, `--doctor` never looked at that directory — so on the one machine
    this ships to, the doctor could report everything healthy while `spanreed
    codex` refused to run, and the only evidence coming back would be a pasted
    log of a passing doctor.
    """

    def test_an_unwritable_log_directory_fails_step_zero(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root ignores the permission bits this test sets")
        root = tmp_path / "state"
        root.mkdir()
        root.chmod(0o500)
        monkeypatch.setenv("SPANREED_STATE_ROOT", str(root))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        buf = io.StringIO()
        try:
            rc = run_doctor(cwd=tmp_path, log_path=tmp_path / "d.log", out=buf)
        finally:
            root.chmod(0o700)
        text = buf.getvalue()
        assert rc == 1
        assert "NOT WRITABLE" in text
        assert "a worker refuses to start without its approval log" in text

    def test_a_writable_log_directory_is_proven_by_writing(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """And the probe leaves nothing behind: a diagnostic that litters the
        directory it is checking is one somebody will later mistake for state."""
        root = tmp_path / "state"
        monkeypatch.setenv("SPANREED_STATE_ROOT", str(root))
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        buf = io.StringIO()
        run_doctor(cwd=tmp_path, log_path=tmp_path / "d.log", out=buf)
        assert "writable (created and removed a probe file there)" in buf.getvalue()
        assert list((root / "codex").iterdir()) == []

    def test_a_cwd_that_cannot_be_written_is_reported_in_write_modes(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        if os.geteuid() == 0:
            pytest.skip("root ignores the permission bits this test sets")
        monkeypatch.setenv("SPANREED_STATE_ROOT", str(tmp_path / "state"))
        monkeypatch.setenv("CODEX_HOME", str(tmp_path))
        # A fake `codex` on PATH, so step 0 gets past the PATH check and reaches
        # the cwd facts at its end.
        bindir = tmp_path / "bin"
        bindir.mkdir()
        fake = bindir / "codex"
        fake.write_text("#!/bin/sh\necho stub-codex 0.0\n")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", str(bindir))
        work = tmp_path / "repo"
        work.mkdir()
        work.chmod(0o500)
        buf = io.StringIO()
        try:
            run_doctor(cwd=work, log_path=tmp_path / "d.log", out=buf, timeout=5.0)
        finally:
            work.chmod(0o700)
        assert "NOT WRITABLE by uid" in buf.getvalue()
