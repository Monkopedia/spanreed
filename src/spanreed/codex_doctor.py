"""``spanreed codex --doctor``: one run, one file, every answer.

This module exists because of how the Codex spike actually went. Thirty runs,
eight wrong causes, and the real one -- a 64KB pipe nobody drained -- sat
visible in every single run as a line reading ``123 lines total`` that never
changed and that nobody read as a measurement. The record is in
``experiments/codex-app-server-spike/README.md``.

The worker now ships to a machine its author cannot debug on. There is no
thirty-run loop available: each iteration costs a PyPI release, and the only
evidence that comes back is whatever a human pastes into a chat window. So the
diagnostics have to be built for that case, and the lessons from the spike are
design constraints here, not anecdotes:

1. **Every probe must be able to fail.** The spike spent twenty-three runs
   printing ``chatgpt.com TCP443 OK`` -- a check that opens a socket and cannot
   detect the thing most likely to be wrong. A probe that cannot report its own
   failure is worse than no probe, because it is read as evidence.
2. **Never report a bare constant.** If a number is the same on every run, that
   is a measurement of something fixed, and it needs enough context for a reader
   to notice. Print denominators and what was expected.
3. **"Did not run" is not "passed".** Every step gets an explicit verdict,
   including ``DID NOT RUN``, so a step skipped because an earlier one failed is
   never mistaken for a step that succeeded.
4. **The whole run goes to a file, and the path is printed last.** Every spike
   run was pasted back as its final screenful, so the inventory at the top --
   the part holding the answers -- was the part that scrolled away.
5. **Say which question a result answers.** A pass on our own thread is not a
   pass on someone else's; the spike printed one as the other for a full run.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO, cast

from .codex_approvals import approval_policy, sandbox_policy, wire_decision
from .codex_client import CodexClient
from .store import default_state_root

MARKER = "SPANREED_DOCTOR_OK"
"""What the model is asked to reply. Specific enough that it cannot appear by
chance, and checked for rather than assumed -- the spike once reported a turn as
driven when only `turn/start` had been *accepted*, with the reply never seen."""


@dataclass
class Step:
    """One question, its verdict, and why -- printed even when it cannot run."""

    n: int
    question: str
    verdict: str = "DID NOT RUN"
    detail: str = "an earlier step did not get far enough"
    answers: str = ""

    def ok(self, detail: str) -> None:
        self.verdict, self.detail = "PASS", detail

    def no(self, detail: str) -> None:
        self.verdict, self.detail = "FAIL", detail

    def warn(self, detail: str) -> None:
        self.verdict, self.detail = "WARN", detail

    def skip(self, detail: str) -> None:
        self.verdict, self.detail = "SKIP", detail


@dataclass
class Report:
    """Accumulates the run. ``facts`` is repeated near the end on purpose."""

    steps: list[Step] = field(default_factory=lambda: [])
    facts: list[str] = field(default_factory=lambda: [])
    out: TextIO = sys.stdout

    def say(self, line: str = "") -> None:
        print(line, file=self.out)

    def rule(self, title: str) -> None:
        self.say(f"\n{'=' * 78}\n{title}\n{'=' * 78}")

    def fact(self, line: str) -> None:
        """A finding worth surviving truncation. Also printed where it happens."""
        self.facts.append(line)
        self.say(f"  {line}")

    def step(self, n: int, question: str) -> Step:
        s = Step(n, question)
        self.steps.append(s)
        return s


def redacted_auth(home: Path) -> dict[str, Any]:
    """Sign-in state as shapes and times. Never a credential value.

    ``auth.json`` holds live tokens. What is diagnostic about it is which keys
    exist, how old it is, and which mode is in force -- a mode name is not a
    secret, and it decides which code path Codex takes.
    """
    out: dict[str, Any] = {"path": str(home / "auth.json"), "exists": False}
    f = home / "auth.json"
    if not f.is_file():
        return out
    st = f.stat()
    out.update(
        exists=True, bytes=st.st_size, age_hours=round((time.time() - st.st_mtime) / 3600, 1)
    )
    try:
        raw: object = json.loads(f.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        out["unreadable"] = str(exc)
        return out
    if not isinstance(raw, dict):
        out["keys"] = type(raw).__name__
        return out
    data = cast("dict[str, object]", raw)
    out["keys"] = sorted(data)
    mode = data.get("auth_mode")
    if isinstance(mode, str):
        out["auth_mode"] = mode
    # Presence only. A boolean here, never the value: this file holds live
    # credentials and a diagnostic that leaks one is worse than no diagnostic.
    tokens = data.get("tokens")
    out["has_tokens_object"] = isinstance(tokens, dict)
    if isinstance(tokens, dict):
        out["token_keys"] = sorted(cast("dict[str, object]", tokens))
    return out


def _config_summary(home: Path) -> dict[str, Any]:
    """The `config.toml` facts that decide whether calls hang.

    The official docs name three reasons `thread/start` blocks: the model
    service, sandbox init, and required MCP servers. All three live here, and
    this file went unread for twenty-four spike runs while being listed by name
    and size in every one of them.
    """
    cfg = home / "config.toml"
    out: dict[str, Any] = {"path": str(cfg), "exists": cfg.is_file()}
    if not cfg.is_file():
        return out
    try:
        text = cfg.read_text(errors="replace")
    except OSError as exc:
        out["unreadable"] = str(exc)
        return out
    out["bytes"] = len(text)
    out["mcp_servers"] = [
        ln.strip() for ln in text.splitlines() if ln.strip().startswith("[mcp_servers")
    ]
    out["required_lines"] = [ln.strip() for ln in text.splitlines() if "required" in ln.lower()]
    timeouts: list[int] = []
    for ln in text.splitlines():
        if "startup_timeout_sec" in ln and "=" in ln:
            raw = ln.split("=", 1)[1].strip().strip('"')
            if raw.isdigit():
                timeouts.append(int(raw))
    out["startup_timeout_sec"] = timeouts
    return out


def probe_writable(directory: Path) -> tuple[bool, str]:
    """Can a file actually be created in ``directory``? Try it and see.

    Not ``os.access``, which answers about permission bits: it says yes on a
    read-only mount and can say no where an ACL says otherwise. The whole point
    of this file is that a probe must be able to report its own failure, and
    the only check that does that here is the write itself.
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".spanreed-doctor-write-probe"
        probe.write_text("probe")
        probe.unlink()
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return True, "created and removed a probe file there"


def _live_codex_processes() -> list[str]:
    """Other codex processes, for context only -- never used as a diagnosis.

    The spike twice blamed contention with these and was twice wrong. They are
    printed because they are the kind of thing a reader wants to see, and
    labelled as context so the next reader does not repeat the mistake.
    """
    try:
        proc = subprocess.run(["pgrep", "-af", "codex"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return []
    lines: list[str] = []
    for ln in (proc.stdout or "").splitlines():
        ln = ln.strip()
        if not ln:
            continue
        if ln.split(None, 1)[0].isdigit() and len(ln.split(None, 1)) == 1:
            # macOS pgrep prints bare pids; ask ps for the command.
            try:
                cmd = subprocess.run(
                    ["ps", "-p", ln, "-o", "command="], capture_output=True, text=True, timeout=10
                )
                ln = f"{ln} {cmd.stdout.strip()}"
            except (OSError, subprocess.SubprocessError):
                pass
        lines.append(ln)
    return lines


def run_doctor(
    *,
    cwd: Path,
    mode: str = "workspace",
    model: str | None = None,
    effort: str | None = None,
    log_path: Path | None = None,
    timeout: float = 240.0,
    turn_timeout: float = 300.0,
    out: TextIO | None = None,
) -> int:
    """Exercise the whole worker path against a real Codex and report.

    Returns 0 only if every step that ran passed.
    """
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_path or (Path.cwd() / f"spanreed-codex-doctor-{stamp}.log")
    fh = log_path.open("w", encoding="utf-8")

    class _Tee:
        def write(self, data: str) -> int:
            (out or sys.stdout).write(data)
            if not fh.closed:
                fh.write(data)
            return len(data)

        def flush(self) -> None:
            (out or sys.stdout).flush()
            if not fh.closed:
                fh.flush()

    rep = Report(out=_Tee())  # type: ignore[arg-type]
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")

    s0 = rep.step(0, "Is there a Codex here, signed in, configured sanely?")
    s1 = rep.step(1, "Does app-server start and complete the handshake?")
    s2 = rep.step(2, "Can we create our own thread with the worker's real params?")
    s3 = rep.step(3, "Does a turn complete and the model actually reply?")
    s4 = rep.step(4, "Does a real approval round-trip work end to end?")
    s4.answers = "whether our decision enum is one the live server accepts"

    rep.rule(f"spanreed codex --doctor   {stamp}")
    rep.say(f"  spanreed  : {__spanreed_version__()}")
    rep.say(f"  python    : {platform.python_version()} on {platform.platform()}")
    rep.say(f"  cwd       : {cwd}")
    rep.say(f"  mode      : {mode}")
    rep.say("  Every step below prints a verdict, including DID NOT RUN. A step that")
    rep.say("  did not execute is never reported as one that passed.")

    # Printed before ANY check that can return early. A test caught this in the
    # opposite order: with codex missing, step 0 bailed and --mode danger warned
    # about nothing. A safety banner conditional on the environment being
    # healthy is a banner that is absent exactly when something is already wrong.
    if mode == "danger":
        rep.say("")
        rep.say("  " + "!" * 70)
        rep.say("  !!  MODE=danger: NO SANDBOX. Any registered agent may wake this worker,")
        rep.say("  !!  and the bus does not authenticate senders. Full machine access.")
        rep.say("  " + "!" * 70)
        rep.fact("MODE=danger - worker runs with dangerFullAccess and no confinement")

    # ---- step 0 -------------------------------------------------------------
    rep.rule("Step 0 - environment")
    # Checked before `codex` on PATH, which returns early: a worker refuses to
    # start when it cannot write its approval log, and that refusal has nothing
    # to do with Codex being installed. A doctor that passes on a machine where
    # the worker cannot start is the exact defect this file exists to avoid.
    log_dir = default_state_root() / "codex"
    writable, why = probe_writable(log_dir)
    if not writable:
        rep.fact(f"worker log directory {log_dir}: NOT WRITABLE - {why}")
        s0.no(
            f"a worker refuses to start without its approval log, and {log_dir} cannot be "
            f"written ({why}). Every approval a worker grants for an unauthenticated sender "
            f"is recorded there. Fix the permissions, or point $SPANREED_STATE_ROOT somewhere "
            f"this user can write."
        )
        return finish(rep, fh, log_path)
    rep.fact(f"worker log directory {log_dir}: writable ({why})")

    codex = shutil.which("codex")
    rep.fact(f"codex on PATH: {codex or 'NOT FOUND'}")
    if not codex:
        s0.no("`codex` is not on PATH; nothing else can be attempted")
        return finish(rep, fh, log_path)
    try:
        ver = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=30)
        rep.fact(f"codex version: {(ver.stdout or ver.stderr).strip() or '(no output)'}")
    except (OSError, subprocess.SubprocessError) as exc:
        rep.fact(f"codex --version failed: {exc}")

    rep.fact(f"CODEX_HOME: {home}  exists={home.exists()}")
    auth = redacted_auth(home)
    rep.fact(f"auth.json: {json.dumps(auth)}")
    cfg = _config_summary(home)
    rep.fact(
        f"config.toml: {len(cfg.get('mcp_servers', []))} MCP server(s), "
        f"{len(cfg.get('required_lines', []))} required line(s), "
        f"startup_timeout_sec={cfg.get('startup_timeout_sec') or 'none'}"
    )
    ceiling = max(cfg.get("startup_timeout_sec") or [0])
    if ceiling and timeout <= ceiling:
        timeout = ceiling * 2.0
        rep.fact(
            f"raising per-call timeout to {timeout:.0f}s: config lets an MCP server take "
            f"{ceiling}s to start, and a shorter fuse measures our patience, not Codex"
        )
    procs = _live_codex_processes()
    rep.say(f"  other codex processes: {len(procs)} (context only, NOT a diagnosis)")
    for p in procs[:8]:
        rep.say(f"    {p[:150]}")

    sandbox = sandbox_policy(cwd, mode)
    approvals = approval_policy(mode)
    rep.fact(f"approvalPolicy we will send: {approvals!r}")
    rep.fact(f"sandboxPolicy we will send: {json.dumps(sandbox)}")
    if not cwd.is_dir():
        s0.no(f"--cwd {cwd} is not a directory")
        return finish(rep, fh, log_path)

    if os.access(cwd, os.W_OK):
        rep.fact(f"--cwd {cwd}: writable by this user")
    else:
        # Not a failure: the worker starts and warns. But in a write mode every
        # file change then fails inside the sandbox, where the only symptom is
        # the model apologising for an edit it could not make.
        rep.fact(
            f"--cwd {cwd}: NOT WRITABLE by uid {os.getuid()} - in mode {mode} every file "
            f"change will fail inside the sandbox and look like the model's own failure"
        )
    s0.ok(f"codex {codex}, CODEX_HOME {home}, mode {mode}")
    return _run_protocol_steps(
        rep,
        fh,
        log_path,
        cwd,
        model,
        effort,
        timeout,
        turn_timeout,
        sandbox,
        approvals,
        (s1, s2, s3, s4),
    )


def __spanreed_version__() -> str:
    try:
        from . import __version__

        return str(__version__)
    except Exception:  # pragma: no cover - version import is not load-bearing
        return "(unknown)"


def finish(rep: Report, fh: TextIO, log_path: Path) -> int:
    """Print the verdicts, the facts, and the log path -- in that order.

    The facts are repeated here because every spike run came back as its last
    screenful. The log path is last because it is the thing a reader needs after
    deciding the run is interesting, and the bottom is the one place that
    survives a copy-paste.
    """
    rep.rule("Key facts (repeated: the top of a long run gets truncated)")
    for line in rep.facts:
        rep.say(f"  {line}")

    rep.rule("Result")
    failures = 0
    for s in rep.steps:
        rep.say(f"  {s.n}. [{s.verdict:11}] {s.question}")
        rep.say(f"        {s.detail}")
        if s.answers:
            rep.say(f"        (answers: {s.answers})")
        failures += s.verdict == "FAIL"

    rep.say("")
    if failures:
        rep.say(f"  {failures} step(s) FAILED. The first failure is the one to read; the")
        rep.say("  steps after it may have been skipped rather than tested.")
    elif any(s.verdict == "DID NOT RUN" for s in rep.steps):
        rep.say("  No failures, but some steps DID NOT RUN. That is not a pass.")
    else:
        rep.say("  Everything passed. A Codex worker can run on this machine.")

    rep.say(f"\n  FULL LOG: {log_path}")
    rep.say("  Send this file. It is self-contained and includes app-server's own output.")
    rep.out.flush()
    if not fh.closed:
        fh.close()
    return 1 if failures else 0


def _run_protocol_steps(
    rep: Report,
    fh: TextIO,
    log_path: Path,
    cwd: Path,
    model: str | None,
    effort: str | None,
    timeout: float,
    turn_timeout: float,
    sandbox: dict[str, Any],
    approvals: str,
    steps: tuple[Step, Step, Step, Step],
) -> int:
    s1, s2, s3, s4 = steps
    seen_requests: list[tuple[str, dict[str, Any]]] = []
    seen_notifications: list[str] = []

    def on_request(method: str, params: dict[str, Any]) -> dict[str, Any] | None:
        """Record every server request, then answer it the way a worker would."""
        seen_requests.append((method, params))
        rep.say(f"    <= SERVER REQUEST  {method}  {json.dumps(params)[:160]}")
        from .codex_approvals import decide

        try:
            d = decide(cwd, method, params)
        except ValueError:
            return None
        try:
            value = wire_decision(method, d.approved)
        except ValueError:
            rep.say(f"       -> no decision enum for {method}; answering -32601")
            return None
        rep.say(f"       -> {value}   ({d.reason[:110]})")
        return {"decision": value}

    def on_note(method: str, params: dict[str, Any]) -> None:
        seen_notifications.append(method)
        rep.say(f"    <- {method}  {json.dumps(params)[:130]}")

    client = CodexClient(
        timeout=timeout,
        turn_timeout=turn_timeout,
        on_server_request=on_request,
        on_notification=on_note,
        # The DOCTOR asks the server to talk; the worker deliberately does not.
        # A worker runs for days and its log volume is a cost. The doctor exists
        # to produce one self-contained file, and app-server's own output is the
        # single most valuable thing in it -- during the spike the answer lived
        # there for thirty runs. Without this the section reads "0 lines", which
        # looks like a silent server rather than a server nobody asked.
        env={"RUST_LOG": os.environ.get("RUST_LOG", "info")},
    )

    try:
        rep.rule("Step 1 - start app-server and handshake")
        try:
            info = client.connect()
            s1.ok(f"initialized; server says {json.dumps(info)[:170]}")
            rep.fact(f"app-server socket: {client.socket_path}")
        except Exception as exc:
            s1.no(f"{type(exc).__name__}: {exc}")
            return finish(rep, fh, log_path)

        rep.rule("Step 2 - create our own thread, with the worker's real params")
        start_params: dict[str, Any] = {"cwd": str(cwd), "approvalPolicy": approvals}
        if model:
            start_params["model"] = model
        rep.say(f"  thread/start {json.dumps(start_params)}")
        try:
            thread = client.thread_start(**start_params)
            thread_id = _thread_id(thread)
            s2.ok(f"thread {thread_id}")
            rep.fact(f"our thread: {thread_id}")
        except Exception as exc:
            s2.no(f"{type(exc).__name__}: {exc}")
            return finish(rep, fh, log_path)

        rep.rule("Step 3 - drive a turn to completion")
        rep.say(f"  asking the model to reply with exactly {MARKER}")
        turn_params: dict[str, Any] = {"sandboxPolicy": sandbox}
        if effort:
            turn_params["effort"] = effort
        if model:
            turn_params["model"] = model
        try:
            client.turn_start(
                thread_id,
                f"Reply with exactly {MARKER} and nothing else. Do not use tools.",
                **turn_params,
            )
            result = client.wait_for_turn()
        except Exception as exc:
            s3.no(f"{type(exc).__name__}: {exc}")
            return finish(rep, fh, log_path)

        body = json.dumps([p for _, p in result.events])
        if MARKER in body:
            s3.ok(f"turn completed and the model replied with {MARKER}")
        elif result.completed:
            s3.warn(
                f"turn reached {result.terminal} but the reply did not contain {MARKER}; "
                f"it ran, the content was unexpected"
            )
        else:
            s3.no(
                f"turn/start was ACCEPTED but no terminal event arrived in {turn_timeout:.0f}s. "
                f"Events: {sorted(set(seen_notifications)) or 'none'}. "
                f"Accepted is not completed."
            )
        rep.fact(f"turn 1 terminal={result.terminal!r} marker_seen={MARKER in body}")

        rep.rule("Step 4 - a REAL approval round-trip")
        rep.say("  This is the step that cannot be tested against a stub: it asks a live")
        rep.say("  server to run a command, so the server itself decides whether the")
        rep.say("  decision value we send back is one it accepts.")
        before = len(seen_requests)
        try:
            client.turn_start(
                thread_id,
                # `pwd` is INSIDE the sandbox, so app-server never asks -- which
                # is exactly what the first live run showed: the command ran, no
                # approval arrived, and the one step that cannot be stubbed went
                # unexercised.
                #
                # The approval channel is for going BEYOND the sandbox
                # (ClientRequest.json documents ApprovalsReviewer as covering
                # "sandbox escapes"). So ask for something outside --cwd, and
                # deliberately a READ of a directory present on every unix: it
                # exercises the escape path without writing anything, on a
                # machine the author cannot inspect.
                f"Run the shell command `ls /etc` -- note that path is OUTSIDE {cwd} -- "
                f"and reply with the first line of its output. If you are not permitted "
                f"to run it, reply with exactly why, in one line.",
                **turn_params,
            )
            r2 = client.wait_for_turn()
        except Exception as exc:
            s4.no(f"{type(exc).__name__}: {exc}")
            return finish(rep, fh, log_path)
        new_requests = seen_requests[before:]
        methods = sorted({m for m, _ in new_requests})
        rep.fact(f"approval requests during the exec turn: {methods or 'NONE'}")
        ran = str(cwd) in json.dumps([p for _, p in r2.events])
        if not new_requests:
            s4.warn(
                "the server asked for no approval at all. Either the model declined to "
                "run a command, or this build does not ask. Not a failure of the "
                "decision encoding -- it was never exercised."
            )
        elif ran:
            s4.ok(
                f"server asked {methods}, we answered "
                f"{wire_decision(methods[0], approved=True)!r}, and the command RAN "
                f"(its output contains {cwd}). The decision enum is correct."
            )
        elif r2.completed:
            s4.no(
                f"server asked {methods} and we answered "
                f"{wire_decision(methods[0], approved=True)!r}, but the command does not "
                f"appear to have run. If the enum member is wrong the server ignores it, "
                f"which looks exactly like this. Compare against ServerRequest.json."
            )
        else:
            s4.no(
                f"exec turn did not reach a terminal state; events {sorted(set(seen_notifications))}"
            )
    finally:
        rep.rule("What app-server itself said")
        log = client.server_log()
        n_lines = len(log.splitlines())
        rep.say(f"  {n_lines} line(s) captured  ({len(log)} bytes)")
        if n_lines == 0:
            rep.say("  NOTHING captured. The doctor sets RUST_LOG=info, so an empty section")
            rep.say("  here means the server wrote nothing at all -- not that it was quiet by")
            rep.say("  choice. Worth reporting: this is the section that answers 'why did the")
            rep.say("  call hang' when one does.")
        else:
            rep.say("  (A line count that is IDENTICAL across runs means a fixed-size buffer,")
            rep.say("   not a quiet server. That mistake cost this project thirty runs.)")
        rep.say("")
        for line in log.splitlines():
            rep.say(f"  | {line}")
        client.close()

    return finish(rep, fh, log_path)


def _thread_id(thread: object) -> str:
    """Pull a thread id out of whatever shape came back.

    thread/start has returned both ``{"thread": {"id": ...}}`` and a flat
    ``{"threadId": ...}`` across versions, so both are read rather than one
    being assumed. An unrecognised shape raises with the payload in the message:
    a doctor that cannot find the id must say what it actually got.
    """
    if isinstance(thread, dict):
        fields = cast("dict[str, object]", thread)
        inner = fields.get("thread")
        if isinstance(inner, dict):
            nested = cast("dict[str, object]", inner)
            for key in ("threadId", "id"):
                value = nested.get(key)
                if isinstance(value, str) and value:
                    return value
        for key in ("threadId", "id"):
            value = fields.get(key)
            if isinstance(value, str) and value:
                return value
    raise ValueError(f"no thread id in {json.dumps(thread)[:200]}")
