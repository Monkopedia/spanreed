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

from pathlib import Path

import pytest

from spanreed.codex_approvals import (
    APPLY_PATCH_APPROVAL,
    ELICITATION_REQUEST,
    EXEC_COMMAND_APPROVAL,
    contains,
    decide,
    sandbox_policy,
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
