"""Waking a sleeper, and the runner that keeps it while it is awake.

``docs/architecture.md``, "Sleepers": "Who does what, without a daemon" and
"Waking". There is no persistent process. Whoever appends mail to a sleeper's
inbox calls :func:`wake_if_needed`, which decides under the sleeper's lock and
launches :func:`run` in a fresh tmux session. The runner exists only while the
agent is awake: it launches Claude as its child, and when Claude exits it
checks the inbox against the transcript, relaunching while anything is unread
and otherwise marking the sleeper asleep.
"""

from __future__ import annotations

import os
import pwd
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from spanreed.sleepers import InboxPosition, SkippedLine, SleeperRecord, WakeFailure
from spanreed.store import StateStore, pid_start_time, process_is_alive, runner_is_alive
from spanreed.transcript import Deliveries, find_transcript, read_deliveries, scan_inbox

POISON_ATTEMPTS = 3
"""Launches where the model ran but the same line was still not delivered,
before that line is skipped (``docs/architecture.md``, the poison-line bound)."""

_PASSED_FROM_PANE = ("TERM", "TMPDIR", "LANG")
"""Locale, terminal and temp dir: taken from the tmux pane, since nothing about
them can carry a credential or another session's identity."""


def tmux_session_name(name: str, generation: int) -> str:
    """New on every wake, so a previous session still tearing down never clashes."""
    return f"spanreed-{name}-{generation}"


@dataclass
class WakeOutcome:
    launched: bool
    reason: str


class LaunchError(RuntimeError):
    """tmux could not start the runner. The reason is what a human should read."""


def _now() -> datetime:
    return datetime.now(UTC)


def _fail(store: StateStore, record: SleeperRecord, reason: str) -> SleeperRecord:
    """Record a wake that did not bring the agent back. Never silent: the reason
    goes in the record and the bus status goes to ``needs_input``. Callers hold
    the sleeper's lock."""
    record = record.model_copy(
        update={
            "state": "asleep",
            "last_wake_failure": WakeFailure(at=_now(), reason=reason),
            "claude_pid": None,
            "claude_pid_start": None,
        }
    )
    store.sleepers.write(record)
    store.set_status(record.agent_id, "needs_input")
    return record


def wake_if_needed(store: StateStore, agent_id: str) -> WakeOutcome | None:
    """Wake ``agent_id`` if it is a sleeper that is not awake. ``None`` if it is not a
    sleeper at all. Never raises: a failed wake must not cost a sender its message,
    which is already in the inbox."""
    record = store.sleeper_for(agent_id)
    if record is None:
        return None
    try:
        return _wake(store, record.name)
    except Exception as exc:  # see the docstring
        reason = f"wake raised {type(exc).__name__}: {exc}"
        try:
            with store.sleepers.lock(record.name):
                current = store.sleepers.read(record.name)
                if current is not None:
                    _fail(store, current, reason)
        except Exception:  # the failure is already being reported
            pass
        return WakeOutcome(False, reason)


def _wake(store: StateStore, name: str) -> WakeOutcome:
    with store.sleepers.lock(name):
        record = store.sleepers.read(name)
        if record is None:
            return WakeOutcome(False, f"sleeper {name!r} was retired")
        if record.state != "asleep" and runner_is_alive(record):
            return WakeOutcome(
                False,
                f"already {record.state} (runner pid {record.runner_pid}, tmux session "
                f"{record.tmux_session})",
            )
        if record.claude_pid is not None and process_is_alive(
            record.claude_pid, record.claude_pid_start
        ):
            # The runner is gone but its Claude is not. A second Claude on the same
            # conversation is worse than a stalled agent, so a human decides.
            reason = (
                f"its runner (pid {record.runner_pid}) died but its Claude (pid "
                f"{record.claude_pid}) is still running; not resuming a second copy of "
                f"the same conversation. Stop pid {record.claude_pid}, then "
                f"`spanreed sleeper wake {name}`."
            )
            _fail(store, record, reason)
            return WakeOutcome(False, reason)
        # Persist the generation BEFORE launching: a waker that died after
        # new-session but before this write would make the next one reuse the
        # session name and fail with "duplicate session".
        generation = record.generation + 1
        session = tmux_session_name(name, generation)
        record = record.model_copy(
            update={"generation": generation, "tmux_session": session, "sleep_requested": False}
        )
        store.sleepers.write(record)
        try:
            pane_pid = _launch_runner(store, record)
        except LaunchError as exc:
            reason = str(exc)
            _fail(store, record, reason)
            return WakeOutcome(False, reason)
        record = record.model_copy(
            update={
                "state": "awake",
                "runner_pid": pane_pid,
                "runner_pid_start": pid_start_time(pane_pid),
            }
        )
        store.sleepers.write(record)
        return WakeOutcome(True, f"launched in tmux session {session} (runner pid {pane_pid})")


def _tmux(record: SleeperRecord) -> str:
    """tmux from the creator's PATH first, then the waker's."""
    found = shutil.which("tmux", path=record.path_env) or shutil.which("tmux")
    if found is None:
        raise LaunchError("tmux not found, on the PATH recorded at create or on this process's own")
    return found


def _launch_runner(store: StateStore, record: SleeperRecord) -> int:
    """Start the runner in a new detached tmux session; return the pane's pid.

    The command is passed as separate arguments, so tmux runs it directly, with no
    shell: the pane's pid is the runner's own.
    """
    assert record.tmux_session is not None
    argv = [
        _tmux(record),
        "new-session",
        "-d",
        "-P",
        "-F",
        "#{pane_pid}",
        "-s",
        record.tmux_session,
        "-c",
        record.cwd,
        "-e",
        f"SPANREED_STATE_ROOT={store.root}",
        record.spanreed_path,
        "sleeper",
        "_run",
        record.name,
        str(record.generation),
    ]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LaunchError(f"could not run tmux: {exc}") from None
    if done.returncode != 0:
        raise LaunchError(
            f"tmux new-session exited {done.returncode}: {done.stderr.strip() or '(no output)'}"
        )
    try:
        return int(done.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise LaunchError(f"tmux printed no pane pid: {done.stdout!r}") from None


def claude_env(store: StateStore, record: SleeperRecord) -> dict[str, str]:
    """The environment Claude is launched with: an allowlist, never what the runner
    inherited minus a few names. The tmux server's environment is whatever its
    first client had: a cron job, an ssh login, another Claude session."""
    account = pwd.getpwuid(os.getuid())
    env = {
        "PATH": record.path_env,
        "HOME": account.pw_dir,
        "USER": account.pw_name,
        "LOGNAME": account.pw_name,
        "SHELL": account.pw_shell,
        "SPANREED_AGENT_NAME": record.name,
        "SPANREED_SLEEPER": "1",
        "SPANREED_STATE_ROOT": str(store.root),
    }
    for key, value in os.environ.items():
        if key in _PASSED_FROM_PANE or key.startswith("LC_"):
            env[key] = value
    return env


def claude_argv(record: SleeperRecord, *, resume: bool) -> list[str]:
    argv = [record.claude_path]
    argv += ["--resume", record.session_id] if resume else ["--session-id", record.session_id]
    argv += ["--permission-mode", record.permission_mode]
    if record.model is not None:
        argv += ["--model", record.model]
    return argv


def _say(text: str) -> None:
    """The runner's log, written to its tmux pane, where `sleeper attach` shows it."""
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    print(f"[{stamp}] spanreed sleeper: {text}", file=sys.stderr, flush=True)


def _claim(store: StateStore, name: str, generation: int) -> SleeperRecord | None:
    """Confirm this process is the runner the record names, or claim a record with
    no live runner. ``None`` means another runner owns it: exit, launch nothing."""
    me = os.getpid()
    with store.sleepers.lock(name):
        record = store.sleepers.read(name)
        if record is None or record.generation != generation:
            return None
        if record.runner_pid not in (None, me) and runner_is_alive(record):
            return None
        record = record.model_copy(
            update={"state": "awake", "runner_pid": me, "runner_pid_start": pid_start_time(me)}
        )
        store.sleepers.write(record)
        return record


def run(store: StateStore, name: str, generation: int) -> int:
    """The runner: launch Claude, and when it exits, relaunch or sleep."""
    # Ctrl-C and Ctrl-Z in the pane are for Claude; the runner must outlive them.
    for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP):
        signal.signal(sig, signal.SIG_IGN)
    record = _claim(store, name, generation)
    if record is None:
        _say(f"not the runner for {name} generation {generation}; another one owns it")
        return 0
    while True:
        before = read_deliveries(find_transcript(record.session_id))
        resume = find_transcript(record.session_id) is not None
        launched_at = time.monotonic()
        try:
            child = subprocess.Popen(
                claude_argv(record, resume=resume),
                cwd=record.cwd,
                env=claude_env(store, record),
                preexec_fn=_restore_signals,
            )
        except OSError as exc:
            with store.sleepers.lock(name):
                current = store.sleepers.read(name)
                if current is not None:
                    _fail(store, current, f"could not launch {record.claude_path}: {exc}")
            _say(f"could not launch Claude: {exc}")
            return 1
        with store.sleepers.lock(name):
            current = store.sleepers.read(name)
            if current is not None:
                store.sleepers.write(
                    current.model_copy(
                        update={
                            "claude_pid": child.pid,
                            "claude_pid_start": pid_start_time(child.pid),
                        }
                    )
                )
        _say(f"Claude started (pid {child.pid}, {'resume' if resume else 'first launch'})")
        returncode = child.wait()
        ran_for = time.monotonic() - launched_at
        after = read_deliveries(find_transcript(record.session_id))
        _say(f"Claude exited with {returncode} after {ran_for:.0f}s")
        next_record = _after_exit(store, name, before, after, returncode)
        if next_record is None:
            return 0
        record = next_record
        _say("unread mail remains; relaunching")


def _restore_signals() -> None:
    for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP):
        signal.signal(sig, signal.SIG_DFL)


def _after_exit(
    store: StateStore, name: str, before: Deliveries, after: Deliveries, returncode: int
) -> SleeperRecord | None:
    """Steps 4 and 5 of "Committing to sleep": under the lock and against the now
    complete transcript, relaunch if anything is unread, otherwise sleep. Returns
    the record to relaunch with, or ``None`` when the runner is done."""
    model_ran = after.real_turns > before.real_turns
    new_errors = {
        error: count - before.error_turns.get(error, 0)
        for error, count in after.error_turns.items()
        if count > before.error_turns.get(error, 0)
    }
    with store.sleepers.lock(name):
        record = store.sleepers.read(name)
        if record is None:
            return None  # retired while awake
        record = record.model_copy(update={"claude_pid": None, "claude_pid_start": None})

        if not model_ran and (new_errors or returncode != 0):
            # A failed wake, not a failed delivery: it never counts toward the
            # poison bound, and the mail stays unread for the next wake.
            errors = ", ".join(f"{count} {error}" for error, count in sorted(new_errors.items()))
            reason = (
                f"the model never ran: {errors}" if errors else f"Claude exited {returncode}"
            ) + " (mail stays unread for the next wake)"
            _fail(store, record, reason)
            _say(f"WAKE FAILED: {reason}")
            return None

        if not after.trustworthy:
            # Fail safe: nothing after read_through counts as read, and nothing is
            # relaunched or skipped, or every check would replay the backlog.
            reason = (
                f"the transcript is {after.status.value}, so what Claude read cannot be "
                f"checked; unread mail will be replayed at the next wake, possibly twice"
            )
            record = record.model_copy(
                update={
                    "state": "asleep",
                    "last_wake_failure": WakeFailure(at=_now(), reason=reason),
                }
            )
            store.sleepers.write(record)
            store.set_status(record.agent_id, "needs_input")
            _say(f"asleep; {reason}")
            return None

        record = _scan_with_poison_bound(store, record, after.delivered, model_ran=model_ran)
        if record.state == "asleep":
            store.sleepers.write(record)
            _say(f"asleep; inbox read through line {record.read_through.line}")
            return None
        if not model_ran:
            # Claude exited by itself, with mail unread, without the model taking
            # a single turn: relaunching would only do the same again, as fast as
            # Claude can start (review of #90 measured 443 launches in 5 s). A
            # failed wake, whatever the exit status.
            reason = (
                f"Claude exited ({returncode}) without taking a turn while inbox line "
                f"{record.undelivered_line} was unread (mail stays unread for the next wake)"
            )
            _fail(store, record, reason)
            _say(f"WAKE FAILED: {reason}")
            return None
        store.sleepers.write(record)
        return record


def _scan_with_poison_bound(
    store: StateStore, record: SleeperRecord, delivered: set[str], *, model_ran: bool
) -> SleeperRecord:
    """The inbox scan, skipping a line only after it has failed to deliver in
    :data:`POISON_ATTEMPTS` launches where the model ran. Returns the record with
    ``state`` set to ``awake`` (relaunch) or ``asleep``."""
    inbox = store.inbox_path(record.agent_id)
    while True:
        scan = scan_inbox(inbox, record.read_through, delivered)
        if scan.first_unread is None:
            return record.model_copy(
                update={
                    "state": "asleep",
                    "read_through": scan.read_through,
                    "undelivered_line": None,
                    "undelivered_attempts": 0,
                }
            )
        attempts = (
            record.undelivered_attempts if record.undelivered_line == scan.first_unread else 0
        )
        if model_ran:
            attempts += 1
        if attempts < POISON_ATTEMPTS:
            return record.model_copy(
                update={
                    "state": "awake",
                    "read_through": scan.read_through,
                    "undelivered_line": scan.first_unread,
                    "undelivered_attempts": attempts,
                }
            )
        skipped_id = scan.first_unread_id
        why = (
            f"not delivered after {attempts} launches in which the model ran; skipped so "
            f"the rest of the inbox can be"
        )
        store.set_status(record.agent_id, "needs_input")
        record = record.model_copy(
            update={
                "read_through": InboxPosition(line=scan.first_unread, msg_id=skipped_id),
                "skipped_lines": [
                    *record.skipped_lines,
                    SkippedLine(line=scan.first_unread, msg_id=skipped_id, at=_now(), why=why),
                ],
                "undelivered_line": None,
                "undelivered_attempts": 0,
            }
        )
        # Only one skip per check: the next line gets its own attempts.
        model_ran = False
