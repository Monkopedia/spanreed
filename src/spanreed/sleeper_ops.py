"""The sleeper operations, shared by the local CLI and a peer's request (#86).

``spanreed sleeper create|list|wake|sleep|retire`` and a ``sleeper-op`` frame
from a conjoined peer run the same functions here, so the remote path cannot
drift from the local one (``docs/architecture.md``, "On the target: who runs
the operation"). Every check the operation needs is made here, not in
argparse: a peer's request never passes through argparse, and the target relies
on nothing the requester checked ("Validation on receipt").

Each operation writes its output through ``out``, a line at a time, and raises
:class:`SleeperError` with the reason when it refuses. The CLI prints as it
goes; a remote request collects the lines into its result.
"""

from __future__ import annotations

import shlex
import shutil
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from spanreed.claude_trust import TrustError, claude_state_file, folder_is_trusted, trust_folder
from spanreed.protocol import validate_agent_id
from spanreed.sleeper_runner import RETIRE_WAIT_S, request_sleep, wait_until_asleep, wake_if_needed
from spanreed.sleepers import (
    DEFAULT_IDLE_TIMEOUT_S,
    PERMISSION_MODES,
    SleeperError,
    SleeperRecord,
    check_claude_args,
    parse_duration,
    sleeper_agent_id,
)
from spanreed.store import StateStore, format_age, runner_is_alive

Out = Callable[[str], None]
"""Where an operation writes its output, one line (or block) per call."""


class _Args(BaseModel):
    """``args`` of a ``sleeper-op``: exactly these fields, of exactly these types."""

    model_config = ConfigDict(extra="forbid", strict=True)


class CreateArgs(_Args):
    """``create``. Every field is required; ``model`` and ``idle_timeout`` may be null."""

    name: str
    cwd: str
    permission_mode: str
    model: str | None
    idle_timeout: str | None
    """As typed (``"15m"``), parsed here; ``None`` for the default."""
    trust: bool
    claude_args: list[str]


class NameArgs(_Args):
    """``wake``, ``sleep`` and ``retire``."""

    name: str


class ListArgs(_Args):
    """``list`` takes nothing."""


def _check_name(name: str) -> str:
    """``name`` if ``agent-<name>`` is a legal bare agent id. It becomes a file name."""
    try:
        validate_agent_id(sleeper_agent_id(name), qualified_ok=False)
    except ValueError as exc:
        raise SleeperError(str(exc)) from None
    return name


def create(
    store: StateStore, args: CreateArgs, out: Out, *, path_env: str, remote: bool = False
) -> int:
    """Record a sleeper. It starts asleep; nothing is launched.

    ``path_env`` is the ``PATH`` that finds ``claude`` and ``spanreed``, recorded
    for every later wake: the creator's own locally, the one ``remote-access on``
    recorded for a peer's request. ``remote`` also refuses a relative ``cwd``,
    which on the target would resolve against wherever its bridge started.
    """
    name = _check_name(args.name)
    agent_id = sleeper_agent_id(name)
    if args.permission_mode not in PERMISSION_MODES:
        raise SleeperError(
            f"--permission-mode {args.permission_mode!r} is not one of "
            f"{', '.join(PERMISSION_MODES)}"
        )
    try:
        idle_timeout_s = (
            DEFAULT_IDLE_TIMEOUT_S
            if args.idle_timeout is None
            else parse_duration(args.idle_timeout)
        )
    except ValueError as exc:
        raise SleeperError(str(exc)) from None
    if remote and not (args.cwd.startswith("/") or args.cwd.startswith("~")):
        raise SleeperError(
            f"--cwd {args.cwd!r} must be absolute or start with ~ in a remote create: a "
            f"relative path has nothing to be relative to on this machine"
        )
    cwd = Path(args.cwd).expanduser().resolve()
    if not cwd.is_dir():
        raise SleeperError(f"--cwd {args.cwd!r} is not a directory")
    claude_args = check_claude_args(list(args.claude_args))
    # Resolved now and used for every launch: a wake may be started by a cron
    # job or a conjoin bridge whose PATH finds neither.
    resolved = {tool: shutil.which(tool, path=path_env) for tool in ("claude", "spanreed")}
    missing = [tool for tool, path in resolved.items() if path is None]
    if missing:
        if remote:
            raise SleeperError(
                f"{' and '.join(missing)} not found on the PATH recorded by `spanreed "
                f"sleeper remote-access on` ({path_env or 'empty'}). Run that again on this "
                f"machine, in a shell whose PATH finds both, to record it again"
            )
        raise SleeperError(
            f"{' and '.join(missing)} not found on PATH; a sleeper runs both by absolute "
            f"path, resolved here at create"
        )
    if args.trust:
        # Before the record exists, so a failure leaves nothing half made.
        try:
            trust_folder(cwd)
        except TrustError as exc:
            raise SleeperError(f"--trust: {exc}") from None
    record = store.create_sleeper(
        SleeperRecord(
            name=name,
            agent_id=agent_id,
            cwd=str(cwd),
            session_id=str(uuid.uuid4()),
            permission_mode=args.permission_mode,
            model=args.model,
            claude_args=claude_args,
            idle_timeout_s=idle_timeout_s,
            claude_path=str(resolved["claude"]),
            spanreed_path=str(resolved["spanreed"]),
            path_env=path_env,
            created_at=datetime.now(UTC),
        )
    )
    out(
        f"created sleeper {record.name} ({record.agent_id}), ASLEEP\n"
        f"  cwd:             {record.cwd}\n"
        f"  session id:      {record.session_id}\n"
        f"  permission mode: {record.permission_mode}\n"
        f"  model:           {record.model or '(Claude Code default)'}\n"
        f"  claude args:     {shlex.join(record.claude_args) or '(none)'}\n"
        f"  idle timeout:    {record.idle_timeout_s}s\n"
        f"  inbox read from: line {record.read_through.line + 1} (earlier mail is history)\n"
        f"It is on the bus now. The first message to it, or `spanreed sleeper wake "
        f"{record.name}`, launches it."
    )
    if args.trust:
        out(f"Marked {cwd} trusted in {claude_state_file()}, as accepting the dialog would.")
    else:
        warning = trust_warning(record)
        if warning:
            out(f"WARNING: {warning}")
    return 0


def trust_warning(record: SleeperRecord) -> str | None:
    """Why this sleeper's first launch would stop at the trust dialog, if it would."""
    trusted = folder_is_trusted(Path(record.cwd))
    if trusted is True:
        return None
    if trusted is None:
        return (
            f"could not read {claude_state_file()} to tell whether Claude Code trusts "
            f"{record.cwd}. If it does not, the first launch stops at its trust dialog."
        )
    return (
        f"Claude Code has not trusted {record.cwd}, so the first launch will stop at its "
        f'"do you trust this folder?" dialog and the sleeper will not take a turn. '
        f"Accept it once with `spanreed sleeper attach {record.name}`, or retire and "
        f"re-create with --trust."
    )


def list_records(store: StateStore) -> list[dict[str, Any]]:
    """Every record, as ``sleeper list --json`` prints them."""
    return [r.model_dump(mode="json") for r in store.sleepers.records()]


def list_sleepers(store: StateStore, out: Out) -> int:
    """``sleeper list``, for a human."""
    records = store.sleepers.records()
    if not records:
        out("no sleepers (create one with `spanreed sleeper create`)")
        return 0
    for r in records:
        state = r.state.upper()
        if r.state != "asleep" and not runner_is_alive(r):
            state += " (runner dead)"
        out(f"{r.name}  {state}  ({r.agent_id})")
        out(f"  cwd:             {r.cwd}")
        out(f"  permission mode: {r.permission_mode}")
        out(f"  model:           {r.model or '(Claude Code default)'}")
        if r.claude_args:
            out(f"  claude args:     {shlex.join(r.claude_args)}")
        out(f"  idle timeout:    {r.idle_timeout_s}s")
        out(f"  session id:      {r.session_id}  (generation {r.generation})")
        if r.tmux_session:
            out(f"  tmux session:    {r.tmux_session}")
        out(f"  read through:    inbox line {r.read_through.line}")
        warning = trust_warning(r)
        if warning:
            out(f"  WARNING: {warning}")
        if r.last_wake_failure:
            out(
                f"  LAST WAKE FAILED {format_age(r.last_wake_failure.at)}: "
                f"{r.last_wake_failure.reason}"
            )
        for skipped in r.skipped_lines:
            out(
                f"  SKIPPED inbox line {skipped.line} ({skipped.msg_id}) "
                f"{format_age(skipped.at)}: {skipped.why}"
            )
    return 0


def wake(store: StateStore, name: str, out: Out) -> int:
    """Wake by hand: the same path a message takes."""
    outcome = wake_if_needed(store, sleeper_agent_id(_check_name(name)))
    if outcome is None:
        raise SleeperError(f"there is no sleeper named {name!r}")
    out(f"sleeper {name}: {outcome.reason}")
    return 0 if outcome.launched or "already" in outcome.reason else 1


def sleep(store: StateStore, name: str, out: Out) -> int:
    """By hand, the same as the agent calling ``request_sleep``."""
    out(f"sleeper {name}: {request_sleep(store, _check_name(name))}")
    return 0


def retire(store: StateStore, name: str, out: Out) -> int:
    """Put it to sleep if awake, waiting up to ``RETIRE_WAIT_S``, then retire it."""
    record = store.sleepers.read(_check_name(name))
    if record is not None and record.state != "asleep" and runner_is_alive(record):
        out(f"sleeper {name} is {record.state}: {request_sleep(store, name)}")
        if not wait_until_asleep(store, name, RETIRE_WAIT_S):
            raise SleeperError(
                f"{name!r} did not go to sleep within {RETIRE_WAIT_S:.0f}s, so it was "
                f"not retired. It sleeps at the end of its current turn; retire it then."
            )
    record = store.retire_sleeper(name)
    out(
        f"retired sleeper {record.name} ({record.agent_id}). Its inbox is kept as "
        f"history; mail to it is now refused, as for any unknown agent."
    )
    return 0
