"""Owner-only modes for everything spanreed writes under its state root (#18).

The bus used to create its state with default modes -- directories 0755 and
files 0644 -- and inherited confinement from ``$HOME`` being 0700, a bit it
neither sets nor checks. The modes here are set explicitly at creation, not by
changing the process umask: umask is process-global, and the Codex worker spawns
``codex``, which would then create the user's project files 0600 too.

Creation sites only make NEW paths private. Paths that already exist are fixed
by :func:`tighten_tree`, run once per session from ``session-start`` -- not from
``StateStore.__init__``, which runs on every MCP tool call (PR #22 priced that
at O(files) per call, forever, to fix a one-time problem).
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import TextIO

DIR_MODE = 0o700
FILE_MODE = 0o600


def private_dir(path: Path, *, parents: bool = False) -> None:
    """``mkdir -p`` where every directory THIS CALL CREATES is 0700.

    Every one, not just the leaf. The first version made only the leaf private,
    so whichever writer touched a fresh bus first -- the MCP log creating
    ``<state>/logs``, say -- created the state root itself as an unprotected
    parent at the umask default. Ancestors that already exist are left exactly
    as they are; an existing directory keeps its mode (that is
    :func:`tighten_tree`'s job).
    """
    if parents:
        missing = [p for p in [path, *path.parents] if not p.exists()]
        for d in reversed(missing):
            d.mkdir(mode=DIR_MODE, exist_ok=True)
    else:
        path.mkdir(mode=DIR_MODE, exist_ok=True)


def open_private(path: Path, mode: str = "a") -> TextIO:
    """Open a text file for append (``"a"``) or overwrite (``"w"``), creating it 0600."""
    if mode not in ("a", "w"):
        raise ValueError(f"open_private supports 'a' or 'w', not {mode!r}")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    return os.fdopen(os.open(path, flags, FILE_MODE), mode)


def write_private_text(path: Path, text: str) -> None:
    """``Path.write_text``, but a newly created file is 0600."""
    with open_private(path, "w") as f:
        f.write(text)


def touch_private(path: Path) -> None:
    """``Path.touch(exist_ok=True)``, but a newly created file is 0600."""
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT, FILE_MODE))


def tighten_tree(root: Path) -> int:
    """Bring every directory under ``root`` to 0700 and every file to 0600.

    Returns how many paths changed. Symlinks are skipped, not followed: a link
    in the state root pointing elsewhere must not get its target chmodded, and
    Linux has no lchmod. A path that vanishes or refuses mid-walk (another
    session deleting a tmp file) is skipped; this is best-effort hardening and
    must never fail the session start that runs it.
    """
    changed = 0
    if not root.is_dir() or root.is_symlink():
        return 0
    # Every real directory is visited exactly once as `dirpath`; os.walk does
    # not descend into a symlinked directory, so those are never touched.
    for dirpath, _dirnames, filenames in os.walk(root):
        targets = [(Path(dirpath), DIR_MODE)] + [(Path(dirpath) / f, FILE_MODE) for f in filenames]
        for path, want in targets:
            try:
                st = os.lstat(path)
                if stat.S_ISLNK(st.st_mode) or stat.S_IMODE(st.st_mode) == want:
                    continue
                os.chmod(path, want)
                changed += 1
            except OSError:
                continue
    return changed
