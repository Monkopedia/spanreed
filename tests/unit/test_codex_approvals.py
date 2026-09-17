"""Tests for the Codex worker approval policy.

This module is the whole containment boundary for a Codex worker: auto-approval
plus "any registered agent may wake it" plus a caller-asserted ``from_agent``
means an unauthenticated inbox write becomes code execution, and ``--cwd`` is
the only thing bounding it (``docs/architecture.md``, "``--cwd`` is the security
boundary"). So these tests are written adversarially — the happy path is three
cases here and every other one is an escape attempt.

The escapes that get their own test are the ones that have historically slipped
past a containment check: ``..`` traversal, a symlink planted inside the tree,
a string-prefix comparison that judges ``/a/foo`` to be inside ``/a/f``, and a
path whose parent does not exist yet (where checking "is the nearest existing
ancestor contained?" answers yes for ``<root>/nope/../../evil``).

What is deliberately *not* asserted anywhere: that approving an
``execCommandApproval`` contains the command. It does not, it cannot, and the
one exec assertion about the approve reason exists to keep the code from
claiming it does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spanreed.codex_approvals import (
    APPLY_PATCH_APPROVAL,
    APPLY_PATCH_APPROVAL_V2,
    CONFINED_MODES,
    ELICITATION_REQUEST,
    EXEC_COMMAND_APPROVAL,
    EXEC_COMMAND_APPROVAL_V2,
    MODES,
    PERMISSIONS_APPROVAL_V2,
    approval_policy,
    contains,
    decide,
    sandbox_mode,
    sandbox_policy,
    wire_decision,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An existing, symlink-free worker ``--cwd``.

    ``tmp_path`` is resolved because on macOS ``/tmp`` is itself a symlink to
    ``/private/tmp``; an unresolved root would compare unequal to every resolved
    target and deny everything, which would make these tests pass for the wrong
    reason.
    """
    d = (tmp_path / "work").resolve()
    d.mkdir()
    return d


class TestContains:
    """The security-critical predicate."""

    def test_root_itself_is_contained(self, root: Path) -> None:
        """An exec whose cwd *is* ``--cwd`` is the ordinary case, not an escape."""
        assert contains(root, root)

    def test_existing_child(self, root: Path) -> None:
        (root / "src").mkdir()
        assert contains(root, root / "src")

    def test_dotdot_escapes(self, root: Path) -> None:
        """``<root>/../evil`` is outside, however innocent the prefix looks."""
        assert not contains(root, root / ".." / "evil")

    def test_absolute_path_outside(self, root: Path) -> None:
        assert not contains(root, Path("/etc/passwd"))

    def test_symlink_inside_pointing_outside(self, root: Path, tmp_path: Path) -> None:
        """A real symlink, because a lexical-only check passes this and leaks the disk.

        Planting a symlink inside ``--cwd`` is the cheapest escape available to
        anyone who can already get one write in: after that, every path *under*
        it looks like a path under ``--cwd``.
        """
        outside = (tmp_path / "outside").resolve()
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        (root / "escape").symlink_to(outside)
        assert not contains(root, root / "escape")
        assert not contains(root, root / "escape" / "secret.txt")

    def test_symlink_inside_pointing_inside(self, root: Path) -> None:
        """The mirror case: resolution must not deny a legitimate in-tree link."""
        (root / "real").mkdir()
        (root / "link").symlink_to(root / "real")
        assert contains(root, root / "link" / "file.txt")

    def test_root_reached_through_a_symlink(self, root: Path, tmp_path: Path) -> None:
        """Both sides resolve, so a symlinked ``--cwd`` still contains its own files."""
        alias = tmp_path / "alias"
        alias.symlink_to(root)
        assert contains(alias, root / "file.txt")

    def test_prefix_collision_is_not_containment(self, tmp_path: Path) -> None:
        """``/a/foo`` is not inside ``/a/f`` — the classic ``str.startswith`` bug.

        Component comparison is the fix; this test is what fails if someone
        "simplifies" it back to a string prefix test.
        """
        base = tmp_path.resolve()
        (base / "f").mkdir()
        (base / "foo").mkdir()
        assert not contains(base / "f", base / "foo")
        assert not contains(base / "f", base / "foo" / "file.txt")
        assert str(base / "foo").startswith(str(base / "f"))  # the bug, spelled out

    def test_not_yet_existing_file_inside(self, root: Path) -> None:
        """A patch creating a new file in a new directory is allowed."""
        assert contains(root, root / "newdir" / "newfile.txt")

    def test_not_yet_existing_file_outside(self, root: Path, tmp_path: Path) -> None:
        assert not contains(root, tmp_path.resolve() / "elsewhere" / "newfile.txt")

    def test_nonexistent_component_cannot_launder_dotdot(self, root: Path) -> None:
        """``<root>/nope/../../evil`` is outside, though its nearest existing ancestor is not.

        This is why the whole path is normalised rather than just its nearest
        existing ancestor: that weaker check finds ``<root>``, sees it contained,
        and approves a write two levels above it.
        """
        assert not contains(root, root / "nope" / ".." / ".." / "evil")

    def test_relative_target_is_relative_to_root(self, root: Path) -> None:
        """Not to the test process's cwd — which is a different directory entirely."""
        assert contains(root, Path("src/main.py"))
        assert not contains(root, Path("../evil"))

    def test_relative_root_raises(self) -> None:
        """A relative ``--cwd`` is a misconfigured worker, not a decline."""
        with pytest.raises(ValueError, match="absolute"):
            contains(Path("work"), Path("/tmp/x"))

    def test_symlink_loop_denies(self, root: Path) -> None:
        """Unresolvable means uncheckable, and uncheckable means denied."""
        (root / "a").symlink_to(root / "b")
        (root / "b").symlink_to(root / "a")
        assert not contains(root, root / "a" / "file.txt")


class TestExecApproval:
    """``execCommandApproval`` — cwd-checkable, effects not. Best-effort only."""

    def test_cwd_inside_approves(self, root: Path) -> None:
        d = decide(root, EXEC_COMMAND_APPROVAL, {"command": ["ls", "-l"], "cwd": str(root)})
        assert d.approved
        assert "ls -l" in d.subject

    def test_approve_reason_does_not_claim_the_command_is_contained(self, root: Path) -> None:
        """The reason must point at ``sandboxPolicy``, because the cwd check bounds nothing.

        ``sh -c 'cd / && rm -rf ~'`` passes this approval with a compliant cwd.
        If the reason text ever starts reading like a containment guarantee, this
        is the test that should have stopped it.
        """
        d = decide(root, EXEC_COMMAND_APPROVAL, {"command": "sh -c 'cd / && ls'", "cwd": str(root)})
        assert d.approved
        assert "sandboxPolicy" in d.reason

    def test_cwd_outside_declines(self, root: Path) -> None:
        d = decide(root, EXEC_COMMAND_APPROVAL, {"command": ["ls"], "cwd": "/etc"})
        assert not d.approved
        assert "/etc" in d.reason

    def test_cwd_dotdot_declines(self, root: Path) -> None:
        d = decide(root, EXEC_COMMAND_APPROVAL, {"command": ["ls"], "cwd": str(root / "..")})
        assert not d.approved

    def test_relative_cwd_inside_approves(self, root: Path) -> None:
        (root / "sub").mkdir()
        d = decide(root, EXEC_COMMAND_APPROVAL, {"command": ["ls"], "cwd": "sub"})
        assert d.approved

    def test_missing_cwd_declines(self, root: Path) -> None:
        """No cwd means nothing to check, and unchecked is not approved."""
        d = decide(root, EXEC_COMMAND_APPROVAL, {"command": ["ls"]})
        assert not d.approved
        assert "cwd" in d.reason

    def test_blank_cwd_declines(self, root: Path) -> None:
        d = decide(root, EXEC_COMMAND_APPROVAL, {"command": ["ls"], "cwd": "  "})
        assert not d.approved

    def test_missing_command_declines(self, root: Path) -> None:
        """An approval that cannot be logged legibly is the one that cannot be reviewed."""
        d = decide(root, EXEC_COMMAND_APPROVAL, {"cwd": str(root)})
        assert not d.approved

    def test_non_string_argv_element_declines(self, root: Path) -> None:
        """Half-readable argv would be logged as something other than what runs."""
        d = decide(root, EXEC_COMMAND_APPROVAL, {"command": ["ls", 7], "cwd": str(root)})
        assert not d.approved

    def test_argv_is_shell_quoted_in_the_log_subject(self, root: Path) -> None:
        d = decide(root, EXEC_COMMAND_APPROVAL, {"command": ["echo", "a b"], "cwd": str(root)})
        assert d.subject == "echo 'a b'"


class TestPatchApproval:
    """``applyPatchApproval`` — here the path check really is the boundary."""

    def test_changes_map_all_inside_approves(self, root: Path) -> None:
        params: dict[str, object] = {
            "changes": {str(root / "a.py"): {"add": 1}, str(root / "b" / "c.py"): {}}
        }
        d = decide(root, APPLY_PATCH_APPROVAL, params)
        assert d.approved
        assert len(d.paths) == 2

    def test_one_path_outside_declines_the_whole_patch(self, root: Path) -> None:
        params: dict[str, object] = {"changes": {str(root / "a.py"): {}, "/etc/passwd": {}}}
        d = decide(root, APPLY_PATCH_APPROVAL, params)
        assert not d.approved
        assert "/etc/passwd" in d.reason

    def test_new_file_in_new_directory_approves(self, root: Path) -> None:
        d = decide(root, APPLY_PATCH_APPROVAL, {"paths": [str(root / "new" / "f.py")]})
        assert d.approved

    def test_dotdot_path_declines(self, root: Path) -> None:
        d = decide(root, APPLY_PATCH_APPROVAL, {"path": str(root / ".." / "evil.py")})
        assert not d.approved

    def test_symlinked_path_out_of_tree_declines(self, root: Path, tmp_path: Path) -> None:
        outside = (tmp_path / "outside").resolve()
        outside.mkdir()
        (root / "escape").symlink_to(outside)
        d = decide(root, APPLY_PATCH_APPROVAL, {"path": str(root / "escape" / "planted.py")})
        assert not d.approved

    def test_file_changes_list_of_objects(self, root: Path) -> None:
        params: dict[str, object] = {
            "fileChanges": [{"path": str(root / "a.py")}, {"path": str(root / "b.py")}]
        }
        assert decide(root, APPLY_PATCH_APPROVAL, params).approved

    def test_no_readable_paths_declines(self, root: Path) -> None:
        """The params shape is unverified, so "we found no path" must never approve."""
        d = decide(root, APPLY_PATCH_APPROVAL, {"someUnknownKey": [{"file": "a.py"}]})
        assert not d.approved

    def test_empty_changes_map_declines(self, root: Path) -> None:
        assert not decide(root, APPLY_PATCH_APPROVAL, {"changes": {}}).approved

    def test_non_string_path_declines(self, root: Path) -> None:
        assert not decide(root, APPLY_PATCH_APPROVAL, {"paths": [{"path": 3}]}).approved


class TestUnknownAndMalformed:
    """Default deny, and — just as important — never raise."""

    def test_unrecognised_method_declines(self, root: Path) -> None:
        d = decide(root, "someNewApproval/request", {"cwd": str(root)})
        assert not d.approved
        assert "unrecognised" in d.reason

    def test_empty_method_declines(self, root: Path) -> None:
        assert not decide(root, "", {}).approved

    def test_elicitation_declines(self, root: Path) -> None:
        """A worker has no human, so the only answer that ends the turn is "no"."""
        d = decide(root, ELICITATION_REQUEST, {"message": "pick one"})
        assert not d.approved
        assert "human" in d.reason

    @pytest.mark.parametrize("params", [None, [], "cwd", 7, {"cwd": 12}])
    def test_malformed_params_decline_without_crashing(self, root: Path, params: object) -> None:
        """Every server request must get an answer; an exception here hangs the worker."""
        for method in (EXEC_COMMAND_APPROVAL, APPLY_PATCH_APPROVAL, ELICITATION_REQUEST, "nope"):
            assert not decide(root, method, params).approved

    def test_relative_root_raises(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            decide(Path("work"), EXEC_COMMAND_APPROVAL, {"command": ["ls"], "cwd": "."})


class TestDecisionLogging:
    """Both outcomes must be loggable — the doc's second non-negotiable rule."""

    def test_approve_line_names_verdict_method_subject_and_reason(self, root: Path) -> None:
        line = decide(root, EXEC_COMMAND_APPROVAL, {"command": ["ls"], "cwd": str(root)}).log_line()
        assert "APPROVE" in line
        assert EXEC_COMMAND_APPROVAL in line
        assert "ls" in line
        assert str(root) in line

    def test_decline_line_names_the_path_it_refused(self, root: Path) -> None:
        line = decide(root, APPLY_PATCH_APPROVAL, {"path": "/etc/passwd"}).log_line()
        assert "DECLINE" in line
        assert "/etc/passwd" in line


class TestSandboxPolicy:
    """The real containment for commands, which this module only points at."""

    def test_carries_the_resolved_root(self, root: Path, tmp_path: Path) -> None:
        """Pins the one fact that is not a guess: the value is the resolved ``--cwd``.

        The key names and mode string are deliberately *not* asserted. They are
        unverified (no ``codex`` on the machine this was written on, so no
        ``generate-json-schema`` to check against), and a test asserting them
        would cement a guess as if it were the schema.
        """
        alias = tmp_path / "alias"
        alias.symlink_to(root)
        assert str(root) in repr(sandbox_policy(alias))

    def test_relative_root_raises(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            sandbox_policy(Path("work"))


class TestModePolicies:
    """The mode -> (approvalPolicy, sandboxPolicy) mapping.

    These assert the exact strings from `SandboxPolicy` and `AskForApproval` in
    ClientRequest.json, because the previous version of `sandbox_policy` was a
    guess with the wrong key AND the wrong value. A test that only checked "the
    root appears somewhere" passed against that guess, which is why it is not
    enough here: app-server silently ignoring an unrecognised policy is the
    failure being guarded, and only the discriminator catches it.
    """

    def test_confined_modes_use_the_schema_discriminator_and_scope_writes(
        self, tmp_path: Path
    ) -> None:
        # Both confined modes, derived: `ask` and `auto` differ in who answers
        # an approval, not in what they ask Codex to confine.
        for mode in CONFINED_MODES:
            pol = sandbox_policy(tmp_path, mode)
            assert pol["type"] == "workspaceWrite", mode
            assert pol["writableRoots"] == [str(tmp_path.resolve())], mode
            assert pol["networkAccess"] is False, mode

    def test_full_is_full_access_and_is_not_downgraded(self, tmp_path: Path) -> None:
        # If a caller asks for full they get full; quietly confining it would
        # make the loud warning a lie.
        assert sandbox_policy(tmp_path, "full") == {"type": "dangerFullAccess"}

    def test_each_mode_sends_the_sandbox_MODE_enum_thread_start_takes(self) -> None:
        # The OTHER sandbox level. thread/start takes the SandboxMode enum and
        # turn/start takes the SandboxPolicy object; a run on 2026-09-17
        # measured the object alone confining nothing, so both are sent and
        # both are pinned. The values are members of SandboxMode in
        # ClientRequest.json -- read, not guessed.
        assert sandbox_mode("ask") == "workspace-write"
        assert sandbox_mode("auto") == "workspace-write"
        assert sandbox_mode("full") == "danger-full-access"

    def test_auto_asks_so_decisions_can_be_logged(self) -> None:
        assert approval_policy("auto") == "on-request"

    def test_full_does_not_ask(self) -> None:
        assert approval_policy("full") == "never"

    def test_ask_sends_the_granular_object_with_sandbox_approval_on(self) -> None:
        # The variant that makes app-server put a sandbox escape to somebody
        # rather than settling it itself. A plain string here would have been
        # accepted by the schema and would have routed nothing to the operator.
        policy = approval_policy("ask")
        assert isinstance(policy, dict)
        granular = policy["granular"]
        assert isinstance(granular, dict)
        assert granular["sandbox_approval"] is True

    def test_the_granular_object_carries_every_field_the_schema_requires(self) -> None:
        """Read out of the vendored schema, not out of a copy of the code.

        An approval policy app-server rejects or ignores leaves the worker on
        whatever default it had, which is the silent-failure shape this project
        keeps hitting -- and an incomplete object is the way to produce one.
        """
        schema = json.loads(
            (
                Path(__file__).parents[2]
                / "experiments"
                / "codex-app-server-spike"
                / "schema"
                / "ClientRequest.json"
            ).read_text()
        )
        variants = schema["definitions"]["AskForApproval"]["oneOf"]
        granular_schema = next(v for v in variants if v.get("title") == "GranularAskForApproval")[
            "properties"
        ]["granular"]
        sent = approval_policy("ask")
        assert isinstance(sent, dict)
        granular = sent["granular"]
        assert isinstance(granular, dict)
        for required in granular_schema["required"]:
            assert required in granular, f"the schema requires {required!r} and it is not sent"
        # And nothing invented: every key sent is a key the schema defines.
        assert set(granular) <= set(granular_schema["properties"])
        for key, value in granular.items():
            assert granular_schema["properties"][key]["type"] == "boolean", key
            assert isinstance(value, bool), key

    def test_unknown_mode_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            sandbox_policy(tmp_path, "yolo")
        with pytest.raises(ValueError):
            approval_policy("yolo")
        with pytest.raises(ValueError):
            sandbox_mode("yolo")

    def test_the_retired_mode_names_are_not_quietly_honoured(self, tmp_path: Path) -> None:
        # `workspace` and `danger` were the names before the modes were made to
        # mirror Codex's own. `danger` falling through to a confined default
        # would be the dangerous direction of that mistake.
        for retired in ("workspace", "danger"):
            with pytest.raises(ValueError):
                sandbox_policy(tmp_path, retired)
            with pytest.raises(ValueError):
                approval_policy(retired)

    def test_confined_modes_is_derived_from_what_each_mode_sends(self) -> None:
        # Not a literal: a hand-written list here has been the source of two
        # defects (a mode added without being looked at, then a rename that
        # left `!= "danger"` matching nothing).
        assert set(CONFINED_MODES) == {
            m for m in MODES if sandbox_policy(Path("/"), m)["type"] != "dangerFullAccess"
        }
        assert "full" not in CONFINED_MODES

    def test_relative_root_still_raises(self) -> None:
        with pytest.raises(ValueError):
            sandbox_policy(Path("rel"), "auto")


class TestV2ApprovalNames:
    """v2 renamed the approval requests; both spellings must decide alike.

    The server reports app_server.api_version="v2", so these are the names a
    real worker will actually receive. Handling only the v1 names would make a
    worker decline every genuine request -- safe, but indistinguishable from a
    deliberate policy, which is the worst kind of bug to debug.
    """

    def test_v2_exec_inside_root_is_approved(self, tmp_path: Path) -> None:
        d = decide(tmp_path, EXEC_COMMAND_APPROVAL_V2, {"command": ["ls"], "cwd": str(tmp_path)})
        assert d.approved

    def test_v2_exec_outside_root_is_declined(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "elsewhere"
        outside.mkdir(exist_ok=True)
        d = decide(tmp_path, EXEC_COMMAND_APPROVAL_V2, {"command": ["ls"], "cwd": str(outside)})
        assert not d.approved

    def test_v2_patch_outside_root_is_declined(self, tmp_path: Path) -> None:
        d = decide(tmp_path, APPLY_PATCH_APPROVAL_V2, {"changes": {"/etc/passwd": {}}})
        assert not d.approved

    def test_permissions_request_is_declined(self, tmp_path: Path) -> None:
        # Not a path-scoped request, so containment cannot judge it. Default deny.
        d = decide(tmp_path, PERMISSIONS_APPROVAL_V2, {})
        assert not d.approved


class TestWireDecisionValues:
    """The value sent back must be a member of the response's own enum.

    Read from the vendored ServerRequest.json. The code previously sent
    "approve", which is in NEITHER enum, so every approval and every decline
    was an invalid value -- the failure being guarded is a worker whose log
    records approvals the server never honoured.
    """

    def test_v1_uses_reviewdecision_spellings(self) -> None:
        # ReviewDecision: approved | approved_for_session |
        # approved_mcp_policy_amendment | timed_out | abort. No "decline".
        assert wire_decision(EXEC_COMMAND_APPROVAL, True) == "approved"
        assert wire_decision(EXEC_COMMAND_APPROVAL, False) == "abort"
        assert wire_decision(APPLY_PATCH_APPROVAL, True) == "approved"
        assert wire_decision(APPLY_PATCH_APPROVAL, False) == "abort"

    def test_v2_uses_accept_decline(self) -> None:
        assert wire_decision(EXEC_COMMAND_APPROVAL_V2, True) == "accept"
        assert wire_decision(APPLY_PATCH_APPROVAL_V2, False) == "decline"

    def test_approve_is_never_emitted(self) -> None:
        # The exact string that was wrong. Belt and braces: if someone
        # reintroduces it, this fails rather than a real server ignoring us.
        every = {
            wire_decision(m, ok)
            for m in (
                EXEC_COMMAND_APPROVAL,
                APPLY_PATCH_APPROVAL,
                EXEC_COMMAND_APPROVAL_V2,
                APPLY_PATCH_APPROVAL_V2,
                PERMISSIONS_APPROVAL_V2,
            )
            for ok in (True, False)
        }
        assert "approve" not in every

    def test_a_method_with_no_enum_raises(self) -> None:
        with pytest.raises(ValueError):
            wire_decision("mcpServer/elicitation/request", True)


class TestFileChangeShapes:
    """v1 names its files; v2 does not. They cannot be judged the same way."""

    def test_v1_filechanges_is_an_object_keyed_by_path(self, tmp_path: Path) -> None:
        # ApplyPatchApprovalParams.fileChanges is
        # {"additionalProperties": {"$ref": "FileChange"}} -- an object whose
        # KEYS are paths. Reading it only as a list made every v1 patch
        # unreadable and therefore declined.
        inside = {str(tmp_path / "a.py"): {"type": "add", "content": "x"}}
        assert decide(tmp_path, APPLY_PATCH_APPROVAL, {"fileChanges": inside}).approved

    def test_v1_filechanges_outside_is_declined(self, tmp_path: Path) -> None:
        outside = tmp_path.parent / "elsewhere"
        outside.mkdir(exist_ok=True)
        changes = {str(outside / "a.py"): {"type": "add", "content": "x"}}
        assert not decide(tmp_path, APPLY_PATCH_APPROVAL, {"fileChanges": changes}).approved

    def test_grant_root_outside_is_refused(self, tmp_path: Path) -> None:
        # grantRoot asks to widen writable access for the rest of the session.
        # Outside --cwd that is exactly the request to refuse.
        assert not decide(tmp_path, APPLY_PATCH_APPROVAL_V2, {"grantRoot": "/"}).approved
        assert not decide(tmp_path, APPLY_PATCH_APPROVAL_V2, {"grantRoot": "/etc"}).approved

    def test_grant_root_inside_is_allowed(self, tmp_path: Path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        assert decide(tmp_path, APPLY_PATCH_APPROVAL_V2, {"grantRoot": str(sub)}).approved

    def test_v2_without_paths_is_approved_on_the_sandbox_authority(self, tmp_path: Path) -> None:
        # FileChangeRequestApprovalParams carries no paths. Declining would make
        # a workspace-mode worker unable to write anything; approving leans on
        # the workspaceWrite sandbox, and the reason must say so out loud.
        d = decide(tmp_path, APPLY_PATCH_APPROVAL_V2, {"itemId": "i", "threadId": "t"})
        assert d.approved
        assert "sandbox" in d.reason

    def test_v1_without_paths_is_still_declined(self, tmp_path: Path) -> None:
        # v1 does name its files, so silence there is malformed, not structural.
        assert not decide(tmp_path, APPLY_PATCH_APPROVAL, {"reason": "x"}).approved


class TestReviewFindings:
    """Regressions for the cross-repo review of #56.

    Each of these is a case where the code did the right thing for a reason it
    described wrongly, or lost a record it promises to keep. The module's own
    standard -- a log that is fiction is worse than no log -- is what makes
    them worth pinning rather than shrugging at.
    """

    def test_an_embedded_nul_produces_a_decision_instead_of_raising(self, tmp_path: Path) -> None:
        # Path.resolve() raises ValueError for an embedded NUL, which was not in
        # contains()'s catch tuple. The request was still answered -- the client
        # wraps handlers and replies -32603 -- but decision.log_line() never ran,
        # so the approval appeared in no log at all.
        d = decide(tmp_path, EXEC_COMMAND_APPROVAL, {"command": ["ls"], "cwd": "/etc\x00/passwd"})
        assert d.approved is False
        assert d.log_line()  # there IS a line to write
        # NOTE: this pins decide() only. That the line reaches the log FILE is
        # pinned in test_codex_worker.py, driving a real worker -- the reviewer
        # of #56 pointed out that this test's original name claimed the second
        # thing while checking the first.

    def test_a_permissions_request_is_not_called_unrecognised(self, tmp_path: Path) -> None:
        # It is a named constant and a member of APPROVAL_METHODS, so telling
        # the operator it was unrecognised misdescribes a case the module
        # explicitly handles.
        d = decide(tmp_path, PERMISSIONS_APPROVAL_V2, {"permissions": ["network"], "cwd": "/tmp"})
        assert d.approved is False
        assert "unrecognised" not in d.reason
        assert "no human" in d.reason
        assert "network" in d.subject

    def test_a_genuinely_unknown_method_is_still_called_unrecognised(self, tmp_path: Path) -> None:
        # The other half: the terminal branch must keep saying what it means for
        # methods that really are unknown, or the fix above has just moved the
        # inaccuracy.
        d = decide(tmp_path, "item/somethingNew/requestApproval", {})
        assert d.approved is False
        assert "unrecognised" in d.reason
