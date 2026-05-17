"""Filesystem-backed state for the Spanreed bus.

State lives under ``~/.claude/spanreed/`` by default:

- ``registry.json``      — current agents (rewritten on every change).
- ``inboxes/<id>.jsonl`` — per-agent append-only message log.
- ``cursors/<id>``       — per-session "last-seen msg_id" markers.

Implementation lives behind a single :class:`StateStore` class so the storage
layout can change without rippling through the MCP server or CLI.
"""

from __future__ import annotations

from pathlib import Path


def default_state_root() -> Path:
    """Where state lives by default. Override via ``SPANREED_STATE_ROOT`` env var."""
    import os

    override = os.environ.get("SPANREED_STATE_ROOT")
    if override:
        return Path(override)
    return Path.home() / ".claude" / "spanreed"


class StateStore:
    """Filesystem-backed state store. See module docstring for layout."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_state_root()
