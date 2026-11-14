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

import contextlib
import os
import pwd
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from spanreed.claude_trust import folder_is_trusted
from spanreed.sleepers import (
    INBOX_MONITOR_COMMAND,
    Activity,
    InboxPosition,
    SkippedLine,
    SleeperError,
    SleeperRecord,
    WakeFailure,
)
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
    launches_without_a_turn = 0
    while True:
        before = read_deliveries(find_transcript(record.session_id))
        resume = find_transcript(record.session_id) is not None
        launched_at = time.monotonic()
        started = _now()
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
        if folder_is_trusted(Path(record.cwd)) is False:
            # Nobody sees a detached pane: say so on the bus.
            _say(
                f"WARNING: Claude Code has not trusted {record.cwd}; this launch is stopped "
                f"at its trust dialog until someone runs `spanreed sleeper attach "
                f"{record.name}` and accepts it"
            )
            store.set_status(record.agent_id, "needs_input")
        returncode, ended_by_runner = _watch(
            store, name, child, started, turns_at_launch=before.real_turns
        )
        ran_for = time.monotonic() - launched_at
        after = read_deliveries(find_transcript(record.session_id))
        _say(f"Claude exited with {returncode} after {ran_for:.0f}s")
        next_record = _after_exit(
            store, name, before, after, returncode, ended_by_runner=ended_by_runner
        )
        if next_record is None:
            return 0
        record = next_record
        if after.real_turns > before.real_turns:
            launches_without_a_turn = 0
        else:
            launches_without_a_turn += 1
        if launches_without_a_turn >= POISON_ATTEMPTS:
            # Unread mail that never starts a turn (a broken Monitor, say) would
            # otherwise relaunch once per idle timeout forever.
            with store.sleepers.lock(name):
                current = store.sleepers.read(name)
                if current is not None:
                    _fail(
                        store,
                        current,
                        f"unread mail (inbox line {current.undelivered_line}) did not start "
                        f"a single turn in {launches_without_a_turn} launches; mail stays "
                        f"unread for the next wake",
                    )
            _say("giving up: unread mail is not reaching Claude")
            return 1
        _say("unread mail remains; relaunching")


RETIRE_WAIT_S = 60.0
"""How long ``retire`` waits for an awake sleeper to go to sleep."""


def record_activity(store: StateStore, name: str, hook: dict[str, object]) -> None:
    """Record a ``UserPromptSubmit`` or ``Stop`` hook as the sleeper's last turn event.

    A ``Notification`` of type ``permission_prompt`` instead sets the bus status to
    ``needs_input``: Claude is waiting on a human, which nobody will see in a
    detached tmux pane unless the bus says so.
    """
    event = hook.get("hook_event_name")
    record = store.sleepers.read(name)
    if record is None:
        return
    session = hook.get("session_id")
    if isinstance(session, str) and session != record.session_id:
        # A Claude the sleeper started (`claude -p` from its Bash tool) inherits
        # SPANREED_SLEEPER and the name, so its hooks land here too. Its Stop is
        # not the sleeper's: counting it would read the sleeper as idle mid-turn.
        return
    if event == "Notification":
        if hook.get("notification_type") == "permission_prompt":
            store.set_status(record.agent_id, "needs_input")
        return
    if event not in ("UserPromptSubmit", "Stop"):
        return
    busy: list[str] = []
    tasks = hook.get("background_tasks")
    for task in cast(list[object], tasks) if isinstance(tasks, list) else []:
        if not isinstance(task, dict):
            continue
        fields = cast(dict[str, object], task)
        if fields.get("status") not in (None, "running"):
            continue
        command = fields.get("command")
        if fields.get("type") == "shell" and command == INBOX_MONITOR_COMMAND:
            continue
        label = command if isinstance(command, str) else fields.get("agent_type")
        busy.append(f"{fields.get('type', 'task')} {label or fields.get('id', '?')}")
    store.sleepers.write_activity(name, Activity(event=str(event), at=_now(), busy=busy))


def request_sleep(store: StateStore, name: str) -> str:
    """Ask sleeper ``name`` to sleep at the end of its turn. Background work still
    running then is stopped (owner's decision 7). Raises ``SleeperError`` if there
    is no such sleeper."""
    with store.sleepers.lock(name):
        record = store.sleepers.read(name)
        if record is None:
            raise SleeperError(f"there is no sleeper named {name!r}")
        if record.state == "asleep":
            return "already asleep"
        store.sleepers.write(record.model_copy(update={"sleep_requested": True}))
    return (
        f"will sleep once its current turn ends and {REQUEST_QUIET_S:.0f}s pass with no "
        f"new turn. Background work still running then is stopped. Mail that arrives "
        f"meanwhile is delivered first: the runner relaunches Claude for it."
    )


def wait_until_asleep(store: StateStore, name: str, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = store.sleepers.read(name)
        if record is None or record.state == "asleep" or not runner_is_alive(record):
            return True
        time.sleep(0.5)
    return False


POLL_S = 2.0
"""How often the runner checks for idleness while Claude runs."""

REQUEST_QUIET_S = 5.0
"""After ``request_sleep``, how long the last ``Stop`` must stand before Claude is
ended: time for a Monitor event queued behind the final turn to start its own."""

END_GRACE_S = 15.0
"""How long Claude gets to exit after SIGTERM before it is killed."""


def sleep_reason(
    record: SleeperRecord, activity: Activity | None, launched_at: datetime, now: datetime
) -> str | None:
    """Why the sleeper should sleep now, or ``None`` if it should not.

    Step 1 of "Committing to sleep". The last turn event must be a ``Stop``, and
    either the agent asked to sleep and a quiet period has passed, or the idle
    timeout has passed with no background work but the inbox Monitor. Activity
    from before this launch does not count; until the first turn, the launch
    itself is the last ``Stop``.
    """
    if activity is None or activity.at < launched_at:
        activity = Activity(event="Stop", at=launched_at)
    if activity.event != "Stop":
        return None
    quiet = (now - activity.at).total_seconds()
    if record.sleep_requested:
        if quiet < REQUEST_QUIET_S:
            return None
        running = f"; stopping {', '.join(activity.busy)}" if activity.busy else ""
        return f"it asked to sleep{running}"
    if activity.busy or quiet < record.idle_timeout_s:
        return None
    return f"idle for {quiet:.0f}s (timeout {record.idle_timeout_s}s)"


def _watch(
    store: StateStore,
    name: str,
    child: subprocess.Popen[bytes],
    launched_at: datetime,
    *,
    turns_at_launch: int,
) -> tuple[int, bool]:
    """Wait for Claude to exit, ending it when :func:`sleep_reason` says so.
    Returns its exit status, and whether the runner ended it."""
    warned = False
    while True:
        try:
            return child.wait(timeout=POLL_S), False
        except subprocess.TimeoutExpired:
            pass
        record = store.sleepers.read(name)
        if record is None or record.state != "awake":
            continue
        activity = store.sleepers.read_activity(name)
        why = sleep_reason(record, activity, launched_at, _now())
        if why is None:
            continue
        if activity is None or activity.at < launched_at:
            # No turn hook has fired this launch. Fine if no turn happened; but if
            # the transcript shows turns, the hooks are not reporting (a plugin
            # older than this spanreed) and "idle" is a guess that could end
            # Claude mid-turn. Stay awake, and say so.
            turns = read_deliveries(find_transcript(record.session_id)).real_turns
            if turns > turns_at_launch:
                if not warned:
                    warned = True
                    reason = (
                        "turns are happening but the plugin's turn hooks report nothing, so "
                        "idleness cannot be judged and this sleeper will not sleep by itself. "
                        "Update the spanreed plugin (`claude plugin update spanreed`)."
                    )
                    _say(f"WARNING: {reason}")
                    store.set_status(record.agent_id, "needs_input")
                continue
        if not _commit_to_sleep(store, name):
            continue
        _say(f"going to sleep: {why}")
        return end_claude(child), True


def _commit_to_sleep(store: StateStore, name: str) -> bool:
    """Step 2: ``state=sleeping`` under the lock. From here a sender appends and does
    nothing else; the runner rechecks the inbox once Claude has exited."""
    with store.sleepers.lock(name):
        record = store.sleepers.read(name)
        if record is None or record.state != "awake" or record.runner_pid != os.getpid():
            return False
        store.sleepers.write(record.model_copy(update={"state": "sleeping"}))
        return True


def _descendants(pid: int) -> list[tuple[int, int | None]]:
    """Every descendant of ``pid`` with its start time, from ``ps`` (no ``/proc``
    needed). The inbox Monitor's ``tail`` runs in a session of its own, so a
    process-group kill would miss it; walking parentage does not."""
    try:
        out = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, check=False
        ).stdout
    except OSError:
        return []
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found: list[int] = []
    stack = list(children.get(pid, []))
    while stack:
        node = stack.pop()
        found.append(node)
        stack.extend(children.get(node, []))
    return [(p, pid_start_time(p)) for p in found]


def end_claude(child: subprocess.Popen[bytes]) -> int:
    """Step 3, "The exit": SIGTERM, which Claude Code handles cleanly (findings
    Test 8); SIGKILL after :data:`END_GRACE_S`. Then anything Claude started that
    outlived it is ended too, so nothing is left tailing the inbox or holding
    resources. Waits until Claude is actually gone; never trusts the signal."""
    family = _descendants(child.pid)
    child.send_signal(signal.SIGTERM)
    try:
        returncode = child.wait(timeout=END_GRACE_S)
    except subprocess.TimeoutExpired:
        _say(f"Claude ignored SIGTERM for {END_GRACE_S:.0f}s; killing it")
        child.kill()
        returncode = child.wait()
    for pid, start in family:
        if process_is_alive(pid, start):
            _say(f"ending pid {pid}, left running by Claude")
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
    return returncode


def _restore_signals() -> None:
    for sig in (signal.SIGINT, signal.SIGQUIT, signal.SIGTSTP):
        signal.signal(sig, signal.SIG_DFL)


def _after_exit(
    store: StateStore,
    name: str,
    before: Deliveries,
    after: Deliveries,
    returncode: int,
    *,
    ended_by_runner: bool = False,
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

        # A signal from the runner makes the exit status nonzero; that is not
        # evidence of a failed launch.
        if not model_ran and (new_errors or (returncode != 0 and not ended_by_runner)):
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
        if not model_ran and not ended_by_runner:
            # Claude exited by itself, with mail unread, without the model taking
            # a single turn: relaunching would only do the same again, as fast as
            # Claude can start (review of #90 measured 443 launches in 5 s). A
            # failed wake, whatever the exit status. (When the runner ended an
            # idle Claude, relaunching is right, and run() bounds how often a
            # turnless launch may repeat.)
            reason = (
                f"Claude exited ({returncode}) without taking a turn while inbox line "
                f"{record.undelivered_line} was unread (mail stays unread for the next wake)"
            )
            _fail(store, record, reason)
            _say(f"WAKE FAILED: {reason}")
            return None
        # The relaunch exists to deliver mail. A request to sleep made before it
        # must not end it: the launch reads as the last Stop, so the request would
        # fire about REQUEST_QUIET_S after launch, before the Monitor has handed
        # anything over (review of #91). The agent can ask again once it has
        # handled the mail.
        record = record.model_copy(update={"sleep_requested": False})
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
