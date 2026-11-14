"""Claude Code's folder trust, read and (on request) written for a sleeper.

The first interactive launch in a folder Claude Code has never trusted stops at
its "do you trust this folder?" dialog (findings Test 12). A sleeper sitting
there never takes a turn, so ``sleeper create --trust`` marks the folder trusted
up front, the way accepting the dialog would.

Claude Code keeps that answer in its state file (``~/.claude.json``, or
``$CLAUDE_CONFIG_DIR/.claude.json``) as ``projects.<dir>.hasTrustDialogAccepted``.
Trust is inherited: a folder under a trusted one opens with no dialog (measured,
findings Test 14). The file is Claude Code's internal format, not a promised
API, and running sessions rewrite it, so everything here fails toward "not
trusted": the worst outcome is the dialog, exactly as without ``--trust``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import cast

TRUST_KEY = "hasTrustDialogAccepted"

_WRITE_ATTEMPTS = 5


class TrustError(RuntimeError):
    """The folder could not be marked trusted. The reason is for a human."""


def claude_state_file() -> Path:
    """Claude Code's state file: ``$CLAUDE_CONFIG_DIR/.claude.json``, else ``~/.claude.json``."""
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(override) / ".claude.json" if override else Path.home() / ".claude.json"


def _projects(state: object) -> dict[str, object] | None:
    if not isinstance(state, dict):
        return None
    projects = cast(dict[str, object], state).get("projects")
    return cast(dict[str, object], projects) if isinstance(projects, dict) else None


def folder_is_trusted(folder: Path, state_file: Path | None = None) -> bool | None:
    """Whether Claude Code would open ``folder`` without the trust dialog.

    ``True`` if it or any parent folder is marked trusted, ``False`` if none is,
    ``None`` if the state file can't be read or isn't the shape expected.
    """
    try:
        state: object = json.loads((state_file or claude_state_file()).read_text())
    except FileNotFoundError:
        return False
    except (OSError, ValueError):
        return None
    projects = _projects(state)
    if projects is None:
        return None if isinstance(state, dict) and "projects" in state else False
    for candidate in (folder, *folder.parents):
        entry = projects.get(str(candidate))
        if isinstance(entry, dict) and cast(dict[str, object], entry).get(TRUST_KEY) is True:
            return True
    return False


def trust_folder(folder: Path, state_file: Path | None = None) -> None:
    """Mark ``folder`` trusted, as accepting the dialog would. Raises :class:`TrustError`.

    Running Claude sessions rewrite the state file, so the write must not undo
    theirs: it re-reads the file, sets the one key, and replaces the file only if
    nothing changed it in between (checked by modification time and size),
    retrying otherwise. Then it reads the file back and confirms.
    """
    path = state_file or claude_state_file()
    for _ in range(_WRITE_ATTEMPTS):
        try:
            before = path.stat()
            state: object = json.loads(path.read_text())
        except FileNotFoundError:
            raise TrustError(
                f"{path} does not exist: Claude Code has not been run as this user yet. "
                f"Run `claude` once, then try again."
            ) from None
        except (OSError, ValueError) as exc:
            raise TrustError(f"cannot read {path}: {exc}") from None
        projects = _projects(state)
        if projects is None:
            raise TrustError(f"{path} has no `projects` table of the shape expected")
        entry = projects.get(str(folder))
        if entry is None:
            entry = {}
            projects[str(folder)] = entry
        if not isinstance(entry, dict):
            raise TrustError(f"{path}: the entry for {folder} is not an object")
        cast(dict[str, object], entry)[TRUST_KEY] = True

        tmp = path.with_name(f"{path.name}.spanreed-{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, before.st_mode & 0o777)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f, indent=2)
        now = path.stat()
        if (now.st_mtime_ns, now.st_size) != (before.st_mtime_ns, before.st_size):
            tmp.unlink()
            continue  # a Claude session wrote it meanwhile: start over from its version
        tmp.replace(path)
        if folder_is_trusted(folder, path) is not True:
            raise TrustError(
                f"wrote {folder} as trusted to {path}, but reading it back does not show it"
            )
        return
    raise TrustError(
        f"{path} kept changing under the write ({_WRITE_ATTEMPTS} attempts); a Claude "
        f"session is rewriting it. Try again, or accept the dialog with `sleeper attach`."
    )
