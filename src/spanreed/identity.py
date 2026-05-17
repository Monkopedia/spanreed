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
