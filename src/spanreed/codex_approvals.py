"""Approval policy for Codex workers — what a worker answers `codex app-server`.

`codex app-server` is bidirectional JSON-RPC: it sends the client *requests*
(``execCommandApproval``, ``applyPatchApproval``,
``mcpServer/elicitation/request``) and blocks until answered. There is no
server-side timeout, so an unanswered request hangs the worker forever — the
failure four other clients shipped (see
``experiments/codex-app-server-spike/README.md``). Every request must therefore
produce a :class:`Decision`, including ones this module does not recognise.

The policy the owner decided (``docs/architecture.md``, "Codex workers"): in
``--mode auto`` — the default — **auto-approve within ``--cwd``, decline outside
it**, and *any* registered agent may wake a worker. In ``--mode ask`` the
operator answers instead, at the worker's terminal; this module still renders
the request and supplies the worker's own view of it, but the verdict is not its
own. ``--mode full`` asks nobody anything, so nothing here runs at all.

Sender identity on this bus is caller-asserted and verified against nothing, so
an unauthenticated inbox write becomes code execution. ``--cwd`` is the scope of
the checks in this module — not a bound on what an approved command then does,
and not something this project claims Codex enforces (``architecture.md``, "What
spanreed does NOT claim"). That is what makes :func:`contains` worth getting
right, and also what keeps it from being a boundary on its own.

**The honest limitation: a path check cannot contain a shell command.**
``execCommandApproval`` hands us a command string and a cwd. The cwd is
checkable; the command's *effects* are not. ``sh -c 'cd / && rm -rf ~'`` has a
perfectly compliant cwd, and nothing in this module would stop it. Approving an
exec here is **best-effort defence-in-depth, not a boundary** — it filters the
obviously-out-of-scope case and makes every command legible in the log. The real
containment for commands is Codex's own ``sandboxPolicy``, which the worker sets
separately at turn level, and which one depends on ``--mode`` -- this module
is mode-blind and must not name it; see
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


# The value that goes back on the wire, per request family. Both were wrong
# before: the code sent "approve", which appears in NEITHER enum.
#
#   v1 ExecCommandApprovalResponse / ApplyPatchApprovalResponse use ReviewDecision:
#       approved | approved_for_session | approved_mcp_policy_amendment
#       | timed_out | abort
#     -- note there is no "decline"; the negative is `abort`.
#   v2 CommandExecutionRequestApprovalResponse / FileChangeRequestApprovalResponse
#     use CommandExecutionApprovalDecision / FileChangeApprovalDecision:
#       accept | acceptForSession | decline | cancel
#
# Read from the vendored ServerRequest.json. An invalid enum value is the worst
# available outcome here: the server either errors or ignores it, and a worker
# that believes it approved something the server never let through is a worker
# whose log is fiction.
_V1_APPROVE, _V1_DENY = "approved", "abort"
_V2_APPROVE, _V2_DENY = "accept", "decline"

_V1_METHODS = frozenset({EXEC_COMMAND_APPROVAL, APPLY_PATCH_APPROVAL})
_V2_METHODS = frozenset(
    {EXEC_COMMAND_APPROVAL_V2, APPLY_PATCH_APPROVAL_V2, PERMISSIONS_APPROVAL_V2}
)
# PERMISSIONS_APPROVAL_V2 is here BY ANALOGY and that is worth stating, because
# it is the same move this module criticises elsewhere. The vendored
# ServerRequest.json defines requests, not responses: it carries
# CommandExecutionApprovalDecision and FileChangeApprovalDecision, and NO
# response enum for item/permissions/requestApproval. So its decision value is
# inferred from its siblings rather than read.
#
# The exposure is bounded: decide() always declines a permissions request, so
# the worst case is a possibly-invalid enum on a request that was going to be
# refused anyway. It fails closed. Confirm against a real server when one is to
# hand -- `spanreed codex --doctor` is where that would show up.


def wire_decision(method: str, approved: bool) -> str:
    """The enum member to send for ``method``. Raises for a method with no enum.

    Kept separate from :func:`decide` because the *policy* (may this happen?)
    and the *spelling* (what does this server call yes?) are different
    questions, and only the second one changes between API versions.
    """
    if method in _V1_METHODS:
        return _V1_APPROVE if approved else _V1_DENY
    if method in _V2_METHODS:
        return _V2_APPROVE if approved else _V2_DENY
    raise ValueError(f"no approval decision enum for {method!r}")


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
    # ValueError joins these because Path.resolve() raises it for an
    # embedded NUL, and a raise here skips decision.log_line() -- the
    # request is still answered (the client wraps handlers and replies
    # -32603), but the approval appears in no log at all, which is the
    # one outcome this module repeatedly says must not happen.
    except (OSError, RuntimeError, ValueError):
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
        return _decide_patch(root, params, method)
    if method == ELICITATION_REQUEST:
        return Decision(
            approved=False,
            method=method,
            subject=_render_elicitation(params),
            reason="a Codex worker has no human attached, so an elicitation cannot be answered; declining ends the turn where silence would hang it",
        )
    if method == PERMISSIONS_APPROVAL_V2:
        # Recognised and declined for a stated reason, which is NOT the same as
        # unrecognised. It previously fell through to the terminal branch and
        # logged "unrecognised approval method" about a method that is a named
        # constant here and a member of APPROVAL_METHODS -- a log line that
        # misdescribes a case the module explicitly handles, which is the same
        # defect class this module was written to stop shipping, one tier down.
        return Decision(
            approved=False,
            method=method,
            subject=_render_permissions(params),
            reason="a permissions request asks a HUMAN to widen what the session may do, and a worker has no human; declining is the answer, not a failure to understand the request",
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


def _decide_patch(root: Path, params: object, method: str = APPLY_PATCH_APPROVAL) -> Decision:
    """A file-change approval: every path it writes must be inside ``root``.

    On **v1** this really is a boundary: ``ApplyPatchApprovalParams.fileChanges``
    is an object keyed by path, so the request names its files and one outside
    ``--cwd`` is refused outright.

    On **v2** it is not. ``FileChangeRequestApprovalParams`` carries
    ``itemId``/``threadId``/``turnId``/``reason``/``startedAtMs`` and a nullable
    ``grantRoot`` — **no paths at all**. When ``grantRoot`` is absent there is
    nothing here to check, and the only thing actually confining the write is
    the ``workspaceWrite`` sandbox with ``writableRoots: [--cwd]`` that the
    worker sets on every turn.

    So v2-without-grantRoot is approved *on the sandbox's authority, not this
    module's*, and says so in its reason. The alternative — declining — makes a
    confined-mode worker unable to write anything, which is safe and useless and
    looks exactly like a deliberate policy rather than a missing field.
    """
    fields = _as_mapping(params)
    if fields is None:
        return _malformed(method)
    paths = _patch_paths(fields)
    if paths is None:
        if method == APPLY_PATCH_APPROVAL_V2:
            return Decision(
                approved=True,
                method=method,
                subject="(v2 file change; params name no path)",
                reason=(
                    "v2 FileChangeRequestApprovalParams carries no paths and no grantRoot, "
                    "so containment here is impossible; approved on the authority of "
                    "whatever sandboxPolicy this worker sent for the turn, which --mode "
                    f"selects (--cwd is {root}). This function is mode-blind and must not "
                    "name a sandbox it cannot know was sent"
                ),
            )
        return Decision(
            approved=False,
            method=method,
            subject="(no readable paths in params)",
            reason="params named no path this policy could read; a patch whose targets are unknown is not approvable",
        )
    subject = " ".join(paths)
    outside = [p for p in paths if not contains(root, Path(p))]
    if outside:
        return Decision(
            approved=False,
            method=method,
            subject=subject,
            reason=f"{len(outside)} of {len(paths)} path(s) outside --cwd {root}: {' '.join(outside)}",
            paths=tuple(paths),
        )
    return Decision(
        approved=True,
        method=method,
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
    # `grantRoot` asks to widen writable access to a whole directory for the
    # rest of the session. On v2 it is the ONLY path-ish field there is --
    # FileChangeRequestApprovalParams carries itemId/threadId/turnId/reason and
    # nothing else -- so a grantRoot outside --cwd is precisely the request to
    # refuse, and its absence leaves nothing for this module to check.
    grant_root = fields.get("grantRoot")
    if isinstance(grant_root, str) and grant_root.strip():
        saw_key = True
        found.append(grant_root)
    # `changes` keyed by path is the shape reported for apply-patch; the others
    # are the obvious variants. All are read defensively: a non-string where a
    # path belongs makes the whole request unreadable rather than half-checked.
    # `fileChanges` is an OBJECT keyed by path -- ApplyPatchApprovalParams says
    # {"additionalProperties": {"$ref": "FileChange"}}. It was read here only as
    # a list, so v1 patches produced no paths and were declined: safe, and
    # indistinguishable from a policy decision.
    for object_key in ("changes", "fileChanges"):
        keyed_value = fields.get(object_key)
        if isinstance(keyed_value, dict):
            saw_key = True
            keyed = cast("dict[object, object]", keyed_value)
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


def _render_permissions(params: object) -> str:
    """Name what was asked for, so the log says more than the method name."""
    fields = _as_mapping(params)
    if fields is None:
        return "(permissions request, unreadable params)"
    perms = fields.get("permissions")
    if isinstance(perms, list):
        items = cast("list[object]", perms)
        named = [str(x) for x in items if isinstance(x, str)]
        if named:
            # Say when the list is cut. This was the only capped renderer in a
            # module whose whole argument is that the approval log must be
            # complete -- _render_command uses shlex.join uncapped and
            # _patch_paths returns every path -- and a silent cap means an
            # operator reading six of nine cannot tell.
            shown = ", ".join(named[:6])
            extra = len(named) - 6
            return f"permissions: {shown}" + (f" (+{extra} more)" if extra > 0 else "")
    cwd = fields.get("cwd")
    return f"permissions request (cwd {cwd})" if isinstance(cwd, str) else "permissions request"


def _render_elicitation(params: object) -> str:
    fields = _as_mapping(params)
    if fields is None:
        return "(malformed params)"
    message = fields.get("message")
    return message if isinstance(message, str) and message.strip() else "(elicitation)"


MODES = ("ask", "auto", "full")
"""``--mode``, mirroring Codex's own three permission modes.

Codex's vocabulary rather than one of ours, deliberately (``architecture.md``,
"Modes"): the thing being configured belongs to Codex, and a name invented here
would have to be kept true to software this project does not control. The
previous pair — ``workspace``/``danger`` — was exactly that invention.

- ``ask``  — workspace-write, granular approvals, answered by the operator at
  the worker's terminal.
- ``auto`` — workspace-write, ``on-request`` approvals answered by
  :func:`decide`. The default.
- ``full`` — ``danger-full-access``, ``never``: nobody is asked anything.
"""

SANDBOX_MODES = {
    # thread/start takes `sandbox`, which is a *SandboxMode enum*, while
    # turn/start takes `sandboxPolicy`, which is the *SandboxPolicy object*
    # sandbox_policy() builds. Different parameter, different type, same
    # concept -- read out of ClientRequest.json rather than guessed, because
    # sending the object under the enum's name is exactly the kind of mistake
    # app-server accepts and ignores.
    #
    # It lives HERE, next to the policy object, rather than in codex_worker,
    # because the worker is no longer the only caller: --doctor sends both
    # levels too, and a doctor holding its own copy of this table is how the
    # doctor came to send no `sandbox` at all while reporting that it used
    # "the worker's real params".
    "ask": "workspace-write",
    "auto": "workspace-write",
    "full": "danger-full-access",
}
"""``--mode`` → ``SandboxMode``, the enum ``thread/start`` accepts."""


def sandbox_mode(mode: str = "auto") -> str:
    """The ``sandbox`` enum member ``thread/start`` takes for ``mode``.

    Both sandbox levels have to be sent, and they are different parameters with
    different types: this one is the enum on ``thread/start``,
    :func:`sandbox_policy` is the object on ``turn/start``. A run on 2026-09-17
    measured the turn-level object *alone* confining nothing.
    """
    if mode not in SANDBOX_MODES:
        raise ValueError(f"mode must be one of {MODES}; got {mode!r}")
    return SANDBOX_MODES[mode]


def sandbox_policy(root: Path, mode: str = "auto") -> dict[str, object]:
    """The ``sandboxPolicy`` a worker sends. Shapes verified against the schema.

    Taken from ``SandboxPolicy`` in ``ClientRequest.json``, produced by ``codex
    app-server generate-json-schema``. The four variants are discriminated by a
    ``type`` field: ``workspaceWrite``, ``readOnly``, ``dangerFullAccess``,
    ``externalSandbox``. Only two are offered as ``--mode``: see MODES.

    An earlier version of this function guessed ``{"mode": "workspace-write"}``
    — wrong key *and* wrong value. That is worth remembering rather than just
    deleting: a policy app-server does not recognise is one it may ignore, and
    an ignored sandbox leaves the worker unconfined while this module's
    best-effort exec check reports that the sandbox is the real boundary. The
    guess would have failed silently, which is the failure mode this package
    exists to stop shipping.

    ``networkAccess`` is left at its schema default of ``False`` for the two
    confined modes: a worker driven by unauthenticated bus mail should not get
    the network thrown in unasked. It is a *boolean* on ``workspaceWrite`` and
    ``readOnly`` and a ``NetworkAccess`` enum on ``externalSandbox`` only —
    checked, because the two spellings are one field name apart.
    """
    if not root.is_absolute():
        raise ValueError(f"root must be absolute; got {root!r}")
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}; got {mode!r}")
    if mode == "full":
        # No confinement at all. The worker is responsible for warning loudly;
        # this function will not silently downgrade the caller's request.
        return {"type": "dangerFullAccess"}
    return {
        "type": "workspaceWrite",
        "writableRoots": [str(root.resolve())],
        "networkAccess": False,
    }


GRANULAR_ASK: dict[str, object] = {
    "granular": {
        # Every member of GranularAskForApproval, spelled as the schema spells
        # them (snake_case here, unlike its camelCase siblings -- read out of
        # ClientRequest.json, not inferred from the neighbours).
        #
        # `mcp_elicitations`, `rules` and `sandbox_approval` are REQUIRED. An
        # object missing one is not a policy app-server can be relied on to
        # apply, and a policy it ignores leaves the worker on whatever default
        # it had -- the silent-failure shape this project keeps paying for.
        #
        # `sandbox_approval` is the one the design names, and `True` is INFERRED
        # to mean "ask about this category" from the field's name alone.
        # ClientRequest.json types all five as bare booleans with no
        # description, so the polarity is not readable from the schema. If
        # `True` in fact means "grant this category without asking", every
        # value in this object is backwards and `ask` is the most permissive
        # confined mode rather than the least. This is the highest-risk unknown
        # in the design; it is item 1 of architecture.md's "Not verifiable
        # here", and a live `--doctor` run is what settles it. Stated here as
        # the inference it is, because the doc hedging while the code asserts is
        # how an inference becomes a fact between two readings.
        "sandbox_approval": True,
        # The other two carry the same inferred polarity, set for the same
        # reason `ask` exists: every category app-server is willing to route to
        # the client should reach the operator rather than being settled
        # somewhere this worker cannot see. Whatever the worker cannot put to a
        # human it declines, so on the assumed polarity the direction is also
        # the closed one.
        "mcp_elicitations": True,
        "rules": True,
        # OPTIONAL, and sent explicitly at their schema defaults rather than
        # left out: what a default is today is a property of a version of
        # codex-cli, and this object is the one place the worker states what it
        # asked for. Both are false because neither is a question this worker
        # can put usefully -- a permission widening and a skill grant outlive
        # the single turn the operator is looking at.
        "request_permissions": False,
        "skill_approval": False,
    }
}
"""The ``granular`` ``AskForApproval`` variant ``ask`` mode sends.

Shape and field names from ``GranularAskForApproval`` in ``ClientRequest.json``.
Kept as data so a test can compare it against the vendored schema's own required
list instead of against a copy of it."""


def approval_policy(mode: str = "auto") -> str | dict[str, object]:
    """The ``approvalPolicy`` for a mode. Values from ``AskForApproval``.

    The schema permits the strings ``"untrusted"``, ``"on-request"`` and
    ``"never"``, or a ``granular`` *object* — so this returns either, and a
    caller that assumed a string would have silently dropped ``ask``.

    ``on-request`` is deliberate for ``auto`` even though the worker
    auto-approves: it is what makes app-server *ask*, which is what makes every
    decision loggable. ``never`` would be less code and would auto-approve just
    the same, but nothing would be written down, and rule 7 wants the owner to
    see what a Codex agent did on their behalf.

    ``ask`` sends the granular object instead, because the operator is the one
    answering and the point is to be asked about as much as app-server will ask
    about. ``full`` sends ``never``: nobody is asked anything.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}; got {mode!r}")
    if mode == "full":
        return "never"
    if mode == "ask":
        return GRANULAR_ASK
    return "on-request"


CONFINED_MODES = tuple(
    m for m in MODES if sandbox_policy(Path("/"), m).get("type") != "dangerFullAccess"
)
"""The modes that ask Codex for *some* confinement.

Derived from the policy each mode actually sends, and named here rather than
written out in a test, because the guard that keeps mode-specific prose honest
has to cover every confined mode including ones that do not exist yet. The
review of #56 added a fourth mode with a freshly-worded false sentence and the
guards passed: they iterated a literal list, so the new mode was simply not
looked at. A hardcoded mode list inside the guard against mode drift is the
thing the guard was supposed to replace — and a list spelled ``!= "danger"``
became wrong the moment the mode was renamed, which is the second way a literal
here has failed. ``Path("/")`` is a placeholder: only the policy's ``type`` is
read, and ``writableRoots`` does not decide whether a mode confines anything."""
