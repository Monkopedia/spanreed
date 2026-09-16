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
import tempfile
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
        assert "did not execute" in rendered
        assert "That is not a pass" in rendered
        assert "2 (DID NOT RUN)" in rendered, "the summary must name WHICH step"

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


class TestSkipIsNotAPass:
    """A SKIPped step used to print "Everything passed".

    `finish()` counted FAIL and the literal "DID NOT RUN"; SKIP fell into the
    else. That mattered little until step 4 gained two skip paths — `--mode
    danger`, and a probe inside a writable root — at which point
    `--doctor --mode danger` reported a clean pass having never exercised the
    approval path at all. Design constraint #3 of the doctor's own docstring.
    """

    def test_a_skipped_step_is_not_reported_as_everything_passing(self) -> None:
        rep = Report(out=io.StringIO())
        rep.steps.append(Step(3, "ran", verdict="PASS", detail="d"))
        rep.steps.append(Step(4, "skipped", verdict="SKIP", detail="mode is danger"))
        rendered = _render(rep)
        assert "Everything passed" not in rendered
        assert "That is not a pass" in rendered
        assert "4 (SKIP)" in rendered

    def test_all_pass_still_says_everything_passed(self) -> None:
        # The other direction: the guard must not make a clean run look dirty.
        rep = Report(out=io.StringIO())
        rep.steps.append(Step(1, "a", verdict="PASS", detail="d"))
        rep.steps.append(Step(2, "b", verdict="PASS", detail="d"))
        assert "Everything passed" in _render(rep)


class TestEscapeVerdictReadsWhatWasSent:
    """The five-way verdict table, driven directly.

    The reviewer's point that these went untested is the reason this exists:
    eight fixes and a near-rewrite of step 4 landed with an empty
    `git diff --stat -- tests/`, and the surviving blocker — a decline erased by
    any co-occurring approval — would have been caught by a two-request fixture
    like `test_a_decline_anywhere_means_the_question_was_not_put` below.

    These are pure functions of data: no live codex, no stub server.
    """

    @staticmethod
    def _run(requests: list[tuple[str, bool, str | None]], *, landed: bool, marker: bool):
        """Drive run_escape_probe with a canned turn and return the Step."""
        from spanreed.codex_client import TurnResult
        from spanreed.codex_doctor import ESCAPE_MARKER, Report, Step, run_escape_probe

        rep = Report(out=io.StringIO())
        s4 = Step(4, "q")
        probe_path = Path()  # rebound below, before any turn runs
        seen: list[tuple[str, dict[str, Any], bool, str | None]] = []

        class _Client:
            def turn_start(self, *a: Any, **k: Any) -> None:
                # The requests arrive DURING the turn, which is what
                # run_escape_probe measures: it takes len(seen) before starting
                # and slices from there. Pre-populating the list made every
                # scenario look like "no approval was requested" -- the fixture
                # tested nothing, and said so by failing.
                seen.extend((m, {}, ok, v) for m, ok, v in requests)
                # And the model's write happens during the turn too. Creating it
                # beforehand did not work: run_escape_probe unlinks the probe
                # before starting, so a pre-written file was deleted and every
                # "landed" scenario tested the "did not land" branch.
                if landed:
                    probe_path.write_text(ESCAPE_MARKER if marker else "something else")

            def wait_for_turn(
                self, *, timeout: float | None = None, on_notify: Any = None
            ) -> TurnResult:
                return TurnResult(
                    completed=True, terminal="turn/completed", terminal_params={}, events=[]
                )

        with tempfile.TemporaryDirectory() as td:
            probe = probe_path = Path(td) / "probe.txt"
            run_escape_probe(
                rep,
                _Client(),
                s4,
                "t",
                {},
                probe,
                Path(td),  # type: ignore[arg-type]
                seen,
                [],
            )
        return s4

    def test_a_decline_anywhere_means_the_question_was_not_put(self) -> None:
        # THE regression. An exec approved (cwd inside --cwd, which bounds
        # nothing about effects) alongside a declined permissions widening --
        # the expected shape of this probe. Previously reported PASS "an
        # approval does not lift the sandbox", from a run where the request that
        # could have lifted it was refused.
        s4 = self._run(
            [
                ("item/commandExecution/requestApproval", True, "accept"),
                ("item/permissions/requestApproval", False, "decline"),
            ],
            landed=False,
            marker=False,
        )
        assert s4.verdict == "WARN"
        assert "DECLINED" in s4.detail
        assert "does not lift the sandbox" not in s4.detail

    def test_all_approved_and_held_is_the_safe_answer(self) -> None:
        s4 = self._run(
            [("item/commandExecution/requestApproval", True, "accept")], landed=False, marker=False
        )
        assert s4.verdict == "PASS"
        assert "does not lift the sandbox" in s4.detail

    def test_all_approved_and_landed_is_the_alarming_answer(self) -> None:
        s4 = self._run(
            [("item/commandExecution/requestApproval", True, "accept")], landed=True, marker=True
        )
        assert s4.verdict == "PASS"
        assert "alarming" in s4.detail

    def test_nothing_sent_on_the_wire_is_not_an_approval(self) -> None:
        # wire_decision raised, so the client answered -32601 and nothing went
        # out. Counting that as an approval is the shape of the original bug.
        s4 = self._run([("item/tool/call", True, None)], landed=False, marker=False)
        assert s4.verdict == "WARN"
        assert "DECLINED" in s4.detail

    def test_a_file_without_the_marker_is_inconclusive(self) -> None:
        s4 = self._run(
            [("item/commandExecution/requestApproval", True, "accept")], landed=True, marker=False
        )
        assert s4.verdict == "WARN"
        assert "inconclusive" in s4.detail
