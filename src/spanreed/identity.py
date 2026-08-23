"""Agent identity derivation for Spanreed.

Two functions, answering two different questions.

:func:`derive_agent_identity` answers "who would a session rooted *here* be?"
— deterministic from cwd plus the ``SPANREED_AGENT_NAME`` override, needing no
shared state, which is what lets the SessionStart hook *mint* an id.

:func:`session_agent_identity` answers "who is the session calling me?" — and
that one does read shared state, because the answer stopped being a property
of the directory the moment the session was free to leave it.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def derive_agent_identity(working_dir: Path | None = None) -> tuple[str, str]:
    """Compute ``(agent_id, name)`` for a session.

    ``SPANREED_AGENT_NAME`` env var overrides both: id becomes
    ``agent-<name>`` and the display name is the override verbatim. Otherwise:

    - name = basename of working dir (or ``"unknown"`` if empty)
    - id   = ``"agent-" + sha256(absolute_path)[:8]``

    Single-session-per-cwd is the documented v1 assumption; override the env
    var if you need multiple sessions in the same directory to be distinct.
    """
    override = os.environ.get("SPANREED_AGENT_NAME")
    if override:
        return f"agent-{override}", override
    wd = (working_dir or Path.cwd()).resolve()
    name = wd.name or "unknown"
    digest = hashlib.sha256(str(wd).encode()).hexdigest()[:8]
    return f"agent-{digest}", name


def session_agent_identity(
    working_dir: Path | None = None,
) -> tuple[str, str, str | None]:
    """Resolve ``(agent_id, name, drift_warning)`` for the *calling session*.

    :func:`derive_agent_identity` answers "who would a session rooted **here**
    be?" — a question about a directory. That is the right question when
    *minting* an identity (``session-start``, ``register``), and the wrong one
    everywhere else: a session that ``cd``s answers it differently after the
    ``cd`` than before, so every later CLI call speaks under an id that is not
    the one the SessionStart hook registered and told the agent to use.

    The anchor is ``$CLAUDE_PID``, which Claude Code sets for the Bash-tool
    processes a session spawns. Every writer records that same value (see
    :func:`session_pid`), so the registry holds a pid-to-identity mapping by
    construction, and this consults it instead of re-deriving from a cwd that
    has since moved. Verified against the fleet: every live entry's ``pid``
    equals its session's ``CLAUDE_PID``.

    Not every spawned process gets it — MCP servers do not — so this is a
    best-effort anchor that degrades to the directory answer, silently and
    indistinguishably from a human at a terminal. That is acceptable only
    because the surfaces that need it (the CLI, the Monitor) do have it;
    ``mcp_server`` needs no anchor at all, its cwd being fixed at spawn.

    Precedence:

    1. ``SPANREED_AGENT_NAME`` — an explicit override outranks everything.
    2. A registry entry whose ``pid`` is ``$CLAUDE_PID`` — this session's
       registered identity, wherever it happens to be standing.
    3. Otherwise :func:`derive_agent_identity` — a human at a terminal has no
       session to belong to, and this path is unchanged for them.

    The third element is a human-readable warning when (2) fired *and*
    disagreed with (1)'s cwd derivation, i.e. the session has drifted. Callers
    print it to stderr: silently doing the right thing would leave an agent
    still believing the id it read from ``pwd``.
    """
    override = os.environ.get("SPANREED_AGENT_NAME")
    if override:
        return f"agent-{override}", override, None

    derived_id, derived_name = derive_agent_identity(working_dir)

    claude_pid = os.environ.get("CLAUDE_PID")
    if not claude_pid or not claude_pid.isdigit():
        return derived_id, derived_name, None

    # Imported here, not at module scope: the SessionStart hook and the Monitor
    # both import this module, and neither should pay for the store on a path
    # that may not need it.
    from spanreed.store import StateStore

    pid = int(claude_pid)
    # Live entries only. ``$CLAUDE_PID`` is our own ancestor, so it is alive by
    # construction, which means the *only* staleness a matching entry can have
    # is a ``pid_start`` mismatch — i.e. exactly pid reuse. Consulting the
    # include-stale view here would therefore buy nothing except the hijack:
    # a dead agent whose pid was recycled onto this session would be adopted as
    # our identity, and the drift warning below would assert it was correct.
    # (A session mid-restart does not need it either: its entry still holds the
    # *old*, dead pid, so it cannot match the new one regardless.)
    owned = [a for a in StateStore().list_agents() if a.pid == pid]
    if len(owned) != 1:
        # Zero: this session has not registered yet (the hook has not run, or
        # this is not a Claude session at all). More than one: the registry is
        # ambiguous about who owns the pid, and guessing would be worse than
        # the status quo. Both fall through to the directory answer.
        return derived_id, derived_name, None

    agent = owned[0]
    if agent.agent_id == derived_id:
        # Both answers agree, so nothing is riding on how we got here.
        return agent.agent_id, agent.name, None

    warning = (
        f"spanreed: this session is registered as {agent.agent_id} ({agent.name}) "
        f"in {agent.working_dir}, but the current directory "
        f"({(working_dir or Path.cwd()).resolve()}) derives {derived_id}. "
        f"Using the registered identity. Bus traffic is unaffected; a command "
        f"run from a different directory no longer changes who you are."
    )
    if agent.pid_start is None:
        # The staleness filter above leans on pid_start to catch pid reuse; with
        # no start time recorded (macOS has no /proc) it could not run, and this
        # match rests on the bare pid alone. Say so rather than let the sentence
        # above sound more certain than it is.
        #
        # Only on the drift path, and the reason is coverage rather than cost:
        # every outcome this note warns about is a *disagreement*. A recycled
        # pid belongs to some other agent's entry, which carries a different
        # working_dir, so it derives a different id — a hijack disagrees by
        # construction and cannot reach the branch above. Restricting the note
        # here therefore gives up nothing; it is not a concession to noise.
        # (Cost is real too — on macOS every entry has pid_start None, so an
        # unconditional note would fire on every call — but that is a bonus,
        # not the argument.)
        warning += (
            " NOTE: no process start time was recorded for this entry, so the"
            " pid-reuse check could not run and this match rests on the pid"
            " alone. See issue #31."
        )
    return agent.agent_id, agent.name, warning


def session_pid() -> int:
    """The pid a registry entry should record for the calling session.

    ``pid`` means *the claude pid of the process whose liveness the entry
    tracks* — settled by the owner on 2026-08-23 (issue #29), and already what
    ``docs/protocol.md`` says for mirrored ``@peer`` entries, which record the
    bridge's pid because the bridge's liveness is theirs.

    ``$CLAUDE_PID`` is that value directly. ``os.getppid()`` is a fallback that
    is right for a human at a terminal — whose parent shell *is* the process
    whose liveness matters — and wrong for every caller inside a session, where
    it is the Bash tool's ``zsh``, alive for the length of one command.

    The SessionStart hook was the one in-session caller ``getppid()`` happened
    to get right, for a reason nothing in the tree recorded. Hooks are declared
    as ``"type": "command"``; a shell running a **single simple command**
    ``exec``s it in place instead of forking, so the parent stays claude.
    Measured on this host (``/bin/sh`` is bash)::

        sh -c "cmd"              ppid = the caller     exec'd in place
        sh -c "cmd || true"      ppid = a doomed shell FORKED
        sh -c "cmd 2>/dev/null"  ppid = a doomed shell FORKED   (bash only)

    The ``||`` result holds in sh, dash, bash and zsh. The redirection result is
    **bash-specific** — dash and zsh still exec in place — so how fragile this
    is depends on which shell runs hooks, which is not something we control or
    have measured. That uncertainty is the argument, not against it: an
    invariant that holds depending on the host's ``/bin/sh`` is not one to
    build identity on. Adding ``|| true`` to ``hooks.json`` would break
    registration for every agent on any shell, silently, with no test failing.
    Reading ``$CLAUDE_PID`` removes the dependency rather than documenting it.
    """
    claude_pid = os.environ.get("CLAUDE_PID")
    # `> 0` matters: pid 0 passes isdigit(), and `os.kill(0, 0)` signals the whole
    # process group rather than probing a process — so an entry recorded with
    # pid 0 reads live forever, which is the fail-open shape this rule exists to
    # prevent. Reject it rather than record an unfalsifiable liveness claim.
    if claude_pid and claude_pid.isdigit() and int(claude_pid) > 0:
        return int(claude_pid)
    return os.getppid()
