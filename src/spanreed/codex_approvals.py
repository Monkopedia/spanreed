"""Approval policy for Codex workers — what a worker answers `codex app-server`.

`codex app-server` is bidirectional JSON-RPC: it sends the client *requests*
(``execCommandApproval``, ``applyPatchApproval``,
``mcpServer/elicitation/request``) and blocks until answered. There is no
server-side timeout, so an unanswered request hangs the worker forever — the
failure four other clients shipped (see
``experiments/codex-app-server-spike/README.md``). Every request must therefore
produce a :class:`Decision`, including ones this module does not recognise.

The policy the owner decided (``docs/architecture.md``, "Codex workers"):
**auto-approve within ``--cwd``, decline outside it**, and *any* registered
agent may wake a worker. Sender identity on this bus is caller-asserted and
verified against nothing, so an unauthenticated inbox write becomes code
execution and ``--cwd`` is the only thing bounding it. That is what makes
:func:`contains` security-critical rather than a convenience.

**The honest limitation: a path check cannot contain a shell command.**
``execCommandApproval`` hands us a command string and a cwd. The cwd is
checkable; the command's *effects* are not. ``sh -c 'cd / && rm -rf ~'`` has a
perfectly compliant cwd, and nothing in this module would stop it. Approving an
exec here is **best-effort defence-in-depth, not a boundary** — it filters the
obviously-out-of-scope case and makes every command legible in the log. The real
containment for commands is Codex's own ``sandboxPolicy``, which the worker sets
separately at turn level (workspace-write scoped to ``--cwd``); see
:func:`sandbox_policy` and read its caveat before trusting its shape.

Two properties hold throughout:

- **Default deny.** An unrecognised method, a malformed params object, a path
  that will not resolve — all decline. An unknown approval type is not an
  approvable one.
- **No I/O beyond path resolution, and no printing.** Decisions are returned as
  data so the caller can log both outcomes (rule 7, and the doc's second
  non-negotiable rule: "approvals are logged, both outcomes"). A policy that
  printed would be a policy that could not be tested quietly.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import cast

EXEC_COMMAND_APPROVAL = "execCommandApproval"
"""Server request asking to run a command. Params carry a command and a cwd."""

APPLY_PATCH_APPROVAL = "applyPatchApproval"
"""Server request asking to write files. Params carry the paths to be written."""

ELICITATION_REQUEST = "mcpServer/elicitation/request"

# app-server speaks v2 to us (the server logs app_server.api_version="v2"), and
# v2 renamed the approval requests. ServerRequest.json lists both spellings, so
# both are handled: an unhandled approval is declined, and a worker that
# declines every real request is useless in a way that looks like a policy
# decision rather than a bug.
EXEC_COMMAND_APPROVAL_V2 = "item/commandExecution/requestApproval"
APPLY_PATCH_APPROVAL_V2 = "item/fileChange/requestApproval"
PERMISSIONS_APPROVAL_V2 = "item/permissions/requestApproval"
"""Server request asking a *human* a question. A worker has none — see :func:`decide`."""


@dataclass(frozen=True)
class Decision:
    """One approval decision, as data the caller logs and answers with.

    ``subject`` is the command or the path list, rendered for a human: the doc
    requires that every approval *and* every decline be reviewable afterwards,
    and an auto-approved command that appears nowhere is exactly the one that
    cannot be reviewed.
    """

    approved: bool
    method: str
    subject: str
    reason: str
    paths: tuple[str, ...] = ()
    """The paths that were containment-checked, exactly as the request named
    them. Empty when the request named none we could find — which is itself
    always a decline."""

    def log_line(self) -> str:
        """A single verbose line naming the verdict, the target and the why."""
        verdict = "APPROVE" if self.approved else "DECLINE"
        return (
            f"[codex-approval] {verdict} {self.method} subject={self.subject} reason={self.reason}"
        )


def contains(root: Path, target: Path) -> bool:
    """Is ``target`` inside ``root``? The security-critical question.

    ``root`` must be absolute — a relative ``--cwd`` would be resolved against
    *this process's* working directory, which is not the worker's, so the answer
    would be about the wrong tree. That is a misconfiguration, not a decline, so
    it raises.

    Both sides are resolved before comparing, which is what makes a symlink
    inside ``--cwd`` pointing at ``/etc`` come out **not** contained. Resolution
    is non-strict so a path that does not exist yet still gets an answer: a
    patch creating ``<root>/newdir/newfile`` is contained, while
    ``<root>/nonexistent/../../evil`` normalises out of the tree and is not.
    (Normalising the whole path is deliberately stricter than checking the
    nearest existing ancestor, which would have said "``<root>`` exists and is
    contained" and approved the escape.)

    Comparison is by path *component*, never by string prefix: ``/a/foo`` is not
    inside ``/a/f``, though ``str.startswith`` says it is.

    ``root`` itself counts as contained — an ``execCommandApproval`` whose cwd is
    the worker's own ``--cwd`` is the ordinary case, and denying it would deny
    every normal command.
    """
    if not root.is_absolute():
        raise ValueError(f"root must be absolute; got {root!r}")
    root_resolved = root.resolve()
    # A relative target means "relative to the worker's cwd". Path.resolve()
    # would instead join it to *this* process's cwd, silently answering about a
    # different directory, so the join is explicit.
    candidate = target if target.is_absolute() else root / target
    try:
        target_resolved = candidate.resolve()
    except (OSError, RuntimeError):
        # Unresolvable means uncheckable, and uncheckable means denied. Both
        # exception types are needed: CPython 3.12 turns a symlink loop's ELOOP
        # into a *RuntimeError* inside resolve(), so catching OSError alone lets
        # it escape into decide() — and an exception there is an unanswered
        # server request, which hangs the worker with no timeout.
        return False
    if _has_unresolved_symlink(target_resolved):
        return False
    return target_resolved == root_resolved or root_resolved in target_resolved.parents


def _has_unresolved_symlink(resolved: Path) -> bool:
    """Did ``resolve()`` quietly give up on a component?

    A successfully resolved path has no symlink anywhere in it, so one that does
    means resolution returned something it did not actually resolve — which is
    what CPython 3.14 does with a symlink loop (3.12 raises instead). Without
    this check the same loop is denied on one interpreter and *contained* on the
    next, and a containment verdict that depends on the Python version is not a
    boundary. Denying costs nothing: such a path cannot be opened either.
    """
    current = resolved
    while True:
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def decide(root: Path, method: str, params: object) -> Decision:
    """Decide one server request. Approve only what is provably inside ``root``.

    Raises :class:`ValueError` if ``root`` is not absolute — see :func:`contains`.
    Everything else about the request is untrusted input and yields a
    :class:`Decision`, never an exception: the caller must be able to answer
    *every* request, and a crash here is a hung worker.
    """
    if not root.is_absolute():
        raise ValueError(f"root must be absolute; got {root!r}")
    if method in (EXEC_COMMAND_APPROVAL, EXEC_COMMAND_APPROVAL_V2):
        return _decide_exec(root, params)
    if method in (APPLY_PATCH_APPROVAL, APPLY_PATCH_APPROVAL_V2):
        return _decide_patch(root, params)
    if method == ELICITATION_REQUEST:
        return Decision(
            approved=False,
            method=method,
            subject=_render_elicitation(params),
            reason="a Codex worker has no human attached, so an elicitation cannot be answered; declining ends the turn where silence would hang it",
        )
    return Decision(
        approved=False,
        method=method,
        subject="(unrecognised request)",
        reason="unrecognised approval method; an unknown approval type is not an approvable one",
    )


def _decide_exec(root: Path, params: object) -> Decision:
    """``execCommandApproval``: check the cwd, log the command, claim nothing more.

    The cwd is the only checkable thing here. The command's effects are not
    bounded by this decision at all — see the module docstring — so the reason
    text says so rather than implying the approval contained anything.
    """
    fields = _as_mapping(params)
    if fields is None:
        return _malformed(EXEC_COMMAND_APPROVAL)
    command = _render_command(fields.get("command"))
    if command is None:
        # Approving a command we cannot render is approving something that
        # appears nowhere in the log, which is the one case the doc's logging
        # rule exists to prevent.
        return Decision(
            approved=False,
            method=EXEC_COMMAND_APPROVAL,
            subject="(no command in params)",
            reason="params carried no readable command, so an approval could not be logged legibly",
        )
    cwd = fields.get("cwd")
    if not isinstance(cwd, str) or not cwd.strip():
        return Decision(
            approved=False,
            method=EXEC_COMMAND_APPROVAL,
            subject=command,
            reason="params carried no cwd, so containment in --cwd could not be checked",
        )
    if not contains(root, Path(cwd)):
        return Decision(
            approved=False,
            method=EXEC_COMMAND_APPROVAL,
            subject=command,
            reason=f"cwd {cwd} is outside --cwd {root}",
            paths=(cwd,),
        )
    return Decision(
        approved=True,
        method=EXEC_COMMAND_APPROVAL,
        subject=command,
        reason=f"cwd {cwd} is inside --cwd {root}; the command's effects are bounded by Codex's sandboxPolicy, not by this check",
        paths=(cwd,),
    )


def _decide_patch(root: Path, params: object) -> Decision:
    """``applyPatchApproval``: every path it writes must be inside ``root``.

    Unlike exec, this one really is a boundary: the request names the files, and
    a file outside ``--cwd`` is refused outright.
    """
    fields = _as_mapping(params)
    if fields is None:
        return _malformed(APPLY_PATCH_APPROVAL)
    paths = _patch_paths(fields)
    if paths is None:
        return Decision(
            approved=False,
            method=APPLY_PATCH_APPROVAL,
            subject="(no readable paths in params)",
            reason="params named no path this policy could read; a patch whose targets are unknown is not approvable",
        )
    subject = " ".join(paths)
    outside = [p for p in paths if not contains(root, Path(p))]
    if outside:
        return Decision(
            approved=False,
            method=APPLY_PATCH_APPROVAL,
            subject=subject,
            reason=f"{len(outside)} of {len(paths)} path(s) outside --cwd {root}: {' '.join(outside)}",
            paths=tuple(paths),
        )
    return Decision(
        approved=True,
        method=APPLY_PATCH_APPROVAL,
        subject=subject,
        reason=f"all {len(paths)} path(s) inside --cwd {root}",
        paths=tuple(paths),
    )


def _malformed(method: str) -> Decision:
    return Decision(
        approved=False,
        method=method,
        subject="(malformed params)",
        reason="params were not an object; a request that cannot be parsed cannot be approved",
    )


def _as_mapping(params: object) -> dict[str, object] | None:
    """JSON params, or ``None`` if the peer sent something else (list, string, null)."""
    if not isinstance(params, dict):
        return None
    # JSON object keys are strings; anything else came from a caller that is not
    # speaking JSON-RPC, and is not to be trusted with a key lookup.
    raw = cast("dict[object, object]", params)
    return {k: v for k, v in raw.items() if isinstance(k, str)}


def _render_command(command: object) -> str | None:
    """Render a command for the log: argv list or plain string, else ``None``."""
    if isinstance(command, str):
        return command if command.strip() else None
    if isinstance(command, list):
        argv = cast("list[object]", command)
        parts = [c for c in argv if isinstance(c, str)]
        if not parts or len(parts) != len(argv):
            # A partially-readable argv would be logged as something other than
            # what runs, which is worse than declining.
            return None
        return shlex.join(parts)
    return None


def _patch_paths(fields: dict[str, object]) -> list[str] | None:
    """Every file path an ``applyPatchApproval`` names, or ``None`` if none read.

    The exact params shape is **unverified** — ``codex`` is not installed on the
    machine this was written on, so the key names below are the plausible ones,
    not schema-confirmed ones (``codex app-server generate-json-schema`` is
    authoritative and on disk wherever codex is). This matters in one direction
    only: if the real shape uses a key not listed here, the paths are unreadable
    and the request is *declined*, which is safe and loud. The direction that is
    genuinely open is a shape that carries paths in **both** a key we read and a
    key we do not — we would then approve on a partial view. Verify the shape
    against the schema before relying on this for anything but declining.
    """
    found: list[str] = []
    saw_key = False
    # `changes` keyed by path is the shape reported for apply-patch; the others
    # are the obvious variants. All are read defensively: a non-string where a
    # path belongs makes the whole request unreadable rather than half-checked.
    changes = fields.get("changes")
    if isinstance(changes, dict):
        saw_key = True
        keyed = cast("dict[object, object]", changes)
        for key in keyed:
            if not isinstance(key, str) or not key.strip():
                return None
            found.append(key)
    for key_name in ("fileChanges", "paths", "files"):
        value = fields.get(key_name)
        if isinstance(value, list):
            saw_key = True
            items = cast("list[object]", value)
            for item in items:
                if isinstance(item, str) and item.strip():
                    found.append(item)
                    continue
                if isinstance(item, dict):
                    entry = cast("dict[object, object]", item)
                    path = entry.get("path")
                    if isinstance(path, str) and path.strip():
                        found.append(path)
                        continue
                return None
    single = fields.get("path")
    if isinstance(single, str) and single.strip():
        saw_key = True
        found.append(single)
    if not saw_key or not found:
        return None
    return found


def _render_elicitation(params: object) -> str:
    fields = _as_mapping(params)
    if fields is None:
        return "(malformed params)"
    message = fields.get("message")
    return message if isinstance(message, str) and message.strip() else "(elicitation)"


MODES = ("read-only", "workspace", "danger")


def sandbox_policy(root: Path, mode: str = "workspace") -> dict[str, object]:
    """The ``sandboxPolicy`` a worker sends. Shapes verified against the schema.

    Taken from ``SandboxPolicy`` in ``ClientRequest.json``, produced by ``codex
    app-server generate-json-schema``. The four variants are discriminated by a
    ``type`` field: ``workspaceWrite``, ``readOnly``, ``dangerFullAccess``,
    ``externalSandbox``.

    An earlier version of this function guessed ``{"mode": "workspace-write"}``
    — wrong key *and* wrong value. That is worth remembering rather than just
    deleting: a policy app-server does not recognise is one it may ignore, and
    an ignored sandbox leaves the worker unconfined while this module's
    best-effort exec check reports that the sandbox is the real boundary. The
    guess would have failed silently, which is the failure mode this package
    exists to stop shipping.

    ``networkAccess`` is left at its schema default of ``False`` for the two
    confined modes: a worker driven by unauthenticated bus mail should not get
    the network thrown in unasked.
    """
    if not root.is_absolute():
        raise ValueError(f"root must be absolute; got {root!r}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}; got {mode!r}")
    if mode == "read-only":
        return {"type": "readOnly", "networkAccess": False}
    if mode == "danger":
        # No confinement at all. The worker is responsible for warning loudly;
        # this function will not silently downgrade the caller's request.
        return {"type": "dangerFullAccess"}
    return {
        "type": "workspaceWrite",
        "writableRoots": [str(root.resolve())],
        "networkAccess": False,
    }


def approval_policy(mode: str = "workspace") -> str:
    """The ``approvalPolicy`` for a mode. Values from ``AskForApproval``.

    The schema permits ``"untrusted"``, ``"on-request"``, ``"never"``, or a
    ``granular`` object.

    ``on-request`` is deliberate for the two confined modes even though the
    worker auto-approves: it is what makes app-server *ask*, which is what makes
    every decision loggable. ``never`` would be less code and would auto-approve
    just the same, but nothing would be written down, and rule 7 wants the owner
    to see what a Codex agent did on their behalf. Confinement comes from the
    sandbox either way, so the choice costs nothing but a round trip.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}; got {mode!r}")
    return "never" if mode == "danger" else "on-request"
