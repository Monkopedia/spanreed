"""Agent identity derivation for Spanreed.

Both the SessionStart hook and the Monitor need to compute the same agent_id
for a given session, without coordinating through a shared file. The trick is
that both derive identity *deterministically* from the same inputs (cwd, plus
the ``SPANREED_AGENT_NAME`` env var override).
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

    The anchor is ``$CLAUDE_PID``, which Claude Code sets in the environment of
    every process a session spawns and which is exactly the pid the SessionStart
    hook records in the registry (it registers with ``os.getppid()``, and Claude
    Code invokes hooks directly). So the registry already holds a
    pid-to-identity mapping; this consults it instead of re-deriving from a cwd
    that has since moved.

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
    # include_stale: a session mid-restart still owns its id, and its entry is
    # keyed by the same live pid we are asking about.
    owned = [a for a in StateStore().list_agents(include_stale=True) if a.pid == pid]
    if len(owned) != 1:
        # Zero: this session has not registered yet (the hook has not run, or
        # this is not a Claude session at all). More than one: the registry is
        # ambiguous about who owns the pid, and guessing would be worse than
        # the status quo. Both fall through to the directory answer.
        return derived_id, derived_name, None

    agent = owned[0]
    if agent.agent_id == derived_id:
        return agent.agent_id, agent.name, None

    warning = (
        f"spanreed: this session is registered as {agent.agent_id} ({agent.name}) "
        f"in {agent.working_dir}, but the current directory "
        f"({(working_dir or Path.cwd()).resolve()}) derives {derived_id}. "
        f"Using the registered identity. Bus traffic is unaffected; a command "
        f"run from a different directory no longer changes who you are."
    )
    return agent.agent_id, agent.name, warning
