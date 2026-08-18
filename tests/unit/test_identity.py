"""Tests for agent identity derivation.

``derive_agent_identity`` mints the id every agent is addressed by, and the id
is the inbox filename — so a change to how it is computed repartitions the bus:
existing inboxes are orphaned and every peer's stored reference goes stale.

These tests exist because the function had none. A mutation sweep found that 5
of 6 deliberate changes to the derivation left the whole suite green, including
``sha256 -> md5`` (every agent gets a new id) and hashing the directory's
basename instead of its full path (every repo sharing a basename collapses into
one agent). Only the ``agent-`` prefix was pinned, by two incidental
``startswith`` assertions elsewhere.

So these assert the derivation *by value*, not by shape. `docs/protocol.md`
documents the id as ``"agent-" + sha256(absolute_cwd)[:8]``, which makes it
wire-format: a second implementation has to agree with it byte for byte.

One caveat found in review, worth stating because it is not obvious: a test that
*recomputes* the digest pins the format only against a change to `identity.py`
alone. It does not survive a **co-edit** — the same `sed` applied to both files
leaves the suite green — and a co-edit is the likely shape when whatever changes
the implementation also repairs the tests it reddens. `docs/protocol.md` does not
rescue it either; nothing executes prose. That is what `test_frozen_vector`
is for, and it is the only assertion here a rename cannot move.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from spanreed.identity import derive_agent_identity


class TestDerivedId:
    """The cwd-derived form: ``agent-<sha256(abspath)[:8]>``."""

    def test_frozen_vector(self) -> None:
        """One hard-coded id for one fixed path — the assertion a rename cannot move.

        Recomputing the digest in the test (as the case below does) pins the
        format only against a change to the *implementation*. It does not survive
        a co-edit: ``sed -i 's/hashlib.sha256/hashlib.md5/'`` across both files
        leaves the suite green while every agent id changes and every inbox is
        orphaned — and a co-edit is the likely shape when the same pass that
        changes the code also fixes the tests it reddens.

        A literal has no expression to rewrite. The path is absolute and need not
        exist, so ``.resolve()`` is a no-op on it and the value is stable across
        machines; the uppercase letters also pin that the hashed input is
        case-sensitive, which nothing else here does.
        """
        assert derive_agent_identity(Path("/spanreed-test/Fixture-Repo"))[0] == "agent-6ef4f0e5"

    def test_id_is_sha256_of_the_absolute_path_truncated_to_eight(self, tmp_path: Path) -> None:
        # Recomputed independently of the function under test, which pins the
        # algorithm, input and truncation against a change to identity.py alone.
        # The frozen vector above is what covers a co-edit of both files.
        expected = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()[:8]
        agent_id, _ = derive_agent_identity(tmp_path)
        assert agent_id == f"agent-{expected}"

    def test_id_has_exactly_eight_hex_digits(self, tmp_path: Path) -> None:
        agent_id, _ = derive_agent_identity(tmp_path)
        digest = agent_id.removeprefix("agent-")
        assert len(digest) == 8
        assert all(c in "0123456789abcdef" for c in digest)

    def test_two_paths_sharing_a_basename_get_different_ids(self, tmp_path: Path) -> None:
        """The full path is hashed, not the directory name.

        Hashing the basename would collapse ``~/git/spanreed`` and
        ``~/work/spanreed`` into one agent sharing one inbox.
        """
        a = tmp_path / "alpha" / "shared"
        b = tmp_path / "beta" / "shared"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        assert a.name == b.name
        assert derive_agent_identity(a)[0] != derive_agent_identity(b)[0]

    def test_relative_path_agrees_with_its_absolute_form(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The path is resolved before hashing.

        Without it, a session launched as ``.`` gets a different id than the
        same session launched by absolute path — silently a second agent.
        """
        monkeypatch.chdir(tmp_path)
        assert derive_agent_identity(Path(".")) == derive_agent_identity(tmp_path)

    def test_symlinked_path_agrees_with_its_target(self, tmp_path: Path) -> None:
        """``.resolve()`` follows symlinks, so reaching a repo through a link
        is the same agent rather than a second one."""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert derive_agent_identity(link)[0] == derive_agent_identity(real)[0]

    def test_same_path_is_stable_across_calls(self, tmp_path: Path) -> None:
        assert derive_agent_identity(tmp_path) == derive_agent_identity(tmp_path)

    def test_defaults_to_the_process_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No argument means live cwd — which is what makes the id follow the
        process around. See #23: a session that changes directory recomputes a
        different id than the one it registered under."""
        monkeypatch.chdir(tmp_path)
        assert derive_agent_identity() == derive_agent_identity(tmp_path)

    def test_name_is_the_directory_basename(self, tmp_path: Path) -> None:
        wd = tmp_path / "my-repo"
        wd.mkdir()
        assert derive_agent_identity(wd)[1] == "my-repo"

    def test_root_directory_falls_back_to_unknown(self) -> None:
        """``Path("/").name`` is empty; the id is still well-formed."""
        agent_id, name = derive_agent_identity(Path("/"))
        assert name == "unknown"
        assert agent_id.startswith("agent-")


class TestEnvOverride:
    """``SPANREED_AGENT_NAME`` replaces both halves, bypassing the digest."""

    def test_override_sets_both_id_and_name(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SPANREED_AGENT_NAME", "alice")
        assert derive_agent_identity(tmp_path) == ("agent-alice", "alice")

    def test_override_ignores_the_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two different cwds under one override are the same agent — which is
        the point of the override, and also why it can collide (see #12)."""
        monkeypatch.setenv("SPANREED_AGENT_NAME", "alice")
        other = tmp_path / "elsewhere"
        other.mkdir()
        assert derive_agent_identity(tmp_path) == derive_agent_identity(other)

    def test_empty_override_falls_through_to_the_digest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Empty string is falsy, so an exported-but-blank var must not produce
        the id ``agent-`` with no digest."""
        monkeypatch.setenv("SPANREED_AGENT_NAME", "")
        # Both assertions do work: the first catches `if override:` becoming
        # `if override is not None:`; the second checks it fell through to the
        # real digest rather than to some other non-"agent-" value.
        assert derive_agent_identity(tmp_path)[0] != "agent-"
        expected = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()[:8]
        assert derive_agent_identity(tmp_path)[0] == f"agent-{expected}"

    def test_unset_override_uses_the_digest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SPANREED_AGENT_NAME", raising=False)
        expected = hashlib.sha256(str(tmp_path.resolve()).encode()).hexdigest()[:8]
        assert derive_agent_identity(tmp_path)[0] == f"agent-{expected}"


def test_both_derivations_share_the_agent_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both forms are ``agent-*``, which is why the two id namespaces overlap.

    Pinned deliberately rather than left implicit: #12 turns on the fact that
    ``SPANREED_AGENT_NAME=<8 hex chars>`` produces a value indistinguishable
    from a digest-derived id.
    """
    monkeypatch.delenv("SPANREED_AGENT_NAME", raising=False)
    assert derive_agent_identity(tmp_path)[0].startswith("agent-")
    monkeypatch.setenv("SPANREED_AGENT_NAME", "deadbeef")
    assert derive_agent_identity(tmp_path) == ("agent-deadbeef", "deadbeef")
