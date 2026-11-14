"""Claude Code's folder trust, read and (on request) written for a sleeper.

The first interactive launch in a folder Claude Code has never trusted stops at
its "do you trust this folder?" dialog (findings Test 12). A sleeper sitting
there never takes a turn, so ``sleeper create --trust`` marks the folder trusted
up front, the way accepting the dialog would.

Claude Code keeps that answer in its state file (``~/.claude.json``, or
``$CLAUDE_CONFIG_DIR/.claude.json``) as ``projects.<dir>.hasTrustDialogAccepted``.
Trust is inherited from parent folders up to the enclosing git repository's
root, and not past it (measured, findings Test 14). The file is Claude Code's internal format, not a promised
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


def _trust_scope(folder: Path) -> list[Path]:
    """The folders whose trust ``folder`` inherits, nearest first: itself and its
    parents, stopping at the enclosing git repository's root. A repository under a
    trusted folder still gets the dialog (measured, findings Test 14)."""
    scope: list[Path] = []
    for candidate in (folder, *folder.parents):
        scope.append(candidate)
        if (candidate / ".git").exists():
            break
    return scope


def folder_is_trusted(folder: Path, state_file: Path | None = None) -> bool | None:
    """Whether Claude Code would open ``folder`` without the trust dialog.

    ``True`` if it, or a parent up to its git root, is marked trusted; ``False`` if
    none is; ``None`` if the state file can't be read or isn't the shape expected.
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
    for candidate in _trust_scope(folder):
        entry = projects.get(str(candidate))
        if isinstance(entry, dict) and cast(dict[str, object], entry).get(TRUST_KEY) is True:
            return True
    return False


def refuse_broad_folder(folder: Path, home: Path | None = None) -> None:
    """Raise :class:`TrustError` for the home folder or anything above it.

    Trust is inherited, so a grant there would cover nearly everything beneath
    it, and Claude Code itself never saves trust for the home folder ("for
    security home-directory trust is never saved"). Granting it here would not
    be "as accepting the dialog would".
    """
    home = (home or Path.home()).resolve()
    if home == folder or home.is_relative_to(folder):
        raise TrustError(
            f"refusing to trust {folder}: it is your home folder or above it, so the "
            f"grant would cover everything under it, and Claude Code never saves trust "
            f"for the home folder itself. Use a project folder."
        )


def _fingerprint(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def trust_folder(folder: Path, state_file: Path | None = None) -> None:
    """Mark ``folder`` trusted, as accepting the dialog would. Raises :class:`TrustError`.

    Running Claude sessions rewrite the state file, and there is no lock shared
    with them, so this **narrows** the chance of undoing their write rather than
    closing it. It re-reads the file, sets the one key, and replaces the file only
    if nothing changed it since the read (inode, size, mtime and ctime), retrying
    otherwise. A session write that lands between that last check and the rename
    is still lost: the review of PR #93 measured 2 of 3,383 writes lost against a
    writer hammering the file at 1 kHz, and never a damaged file. Then it reads the file back to confirm.

    A symlinked state file (a dotfile manager's) is written through: the temp
    file goes next to the real target, so the link stays a link.
    """
    refuse_broad_folder(folder)
    path = (state_file or claude_state_file()).resolve()
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
        created = False
        try:
            fd = os.open(
                tmp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                before.st_mode & 0o777,
            )
            created = True
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                # Claude Code's own layout, so unrelated bytes round-trip unchanged.
                json.dump(state, f, indent=2, ensure_ascii=False)
            if _fingerprint(path.stat()) != _fingerprint(before):
                tmp.unlink()
                continue  # a Claude session wrote it meanwhile: start over from its version
            tmp.replace(path)
        except OSError as exc:
            if created:  # never remove a file this call did not make
                tmp.unlink(missing_ok=True)
            raise TrustError(f"cannot write {path}: {exc}") from None
        if folder_is_trusted(folder, path) is not True:
            raise TrustError(
                f"wrote {folder} as trusted to {path}, but reading it back does not show it"
            )
        return
    raise TrustError(
        f"{path} kept changing under the write ({_WRITE_ATTEMPTS} attempts); a Claude "
        f"session is rewriting it. Try again, or accept the dialog with `sleeper attach`."
    )
