"""Agent ids are validated wherever they enter or become a path (#12).

Each class below is one of the issue's three reproductions, run against the
real boundary rather than the validator alone: a validator nothing calls would
pass its own tests and fix none of them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from spanreed.identity import derive_agent_identity
from spanreed.protocol import Agent, Message, validate_agent_id
from spanreed.store import StateStore


@pytest.fixture
def store(tmp_path: Path) -> StateStore:
    return StateStore(root=tmp_path / "state")


class TestTheValidator:
    @pytest.mark.parametrize(
        "agent_id",
        [
            "agent-04610969",
            "agent-worker",
            "agent-my.bot_2",
            "agent-x@adolin",
            "agent-x@adolin.lan",
        ],
    )
    def test_accepts_the_shapes_the_bus_actually_uses(self, agent_id: str) -> None:
        assert validate_agent_id(agent_id) == agent_id

    @pytest.mark.parametrize(
        "agent_id",
        [
            "../../victim",
            "agent-a/b",
            "a@b@c",
            "@host",
            "agent-x@",
            ".hidden",
            "",
            "a b",
            "x" * 201,
        ],
    )
    def test_rejects_traversal_and_malformed(self, agent_id: str) -> None:
        with pytest.raises(ValueError):
            validate_agent_id(agent_id)

    def test_at_is_refused_where_only_a_local_id_is_legitimate(self) -> None:
        with pytest.raises(ValueError, match="reserved for bridge-qualified"):
            validate_agent_id("agent-worker@adolin", qualified_ok=False)


class TestALocalAgentCannotClaimToBeRemote:
    """Reproduction 1: an '@' in a local id got it evicted by a peer's sync."""

    def test_the_name_override_cannot_mint_an_at_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SPANREED_AGENT_NAME", "worker@adolin")
        with pytest.raises(ValueError, match="reserved"):
            derive_agent_identity()

    def test_register_refuses_an_at_id(self, store: StateStore) -> None:
        with pytest.raises(ValueError, match="reserved"):
            store.register_agent(name="w", working_dir="/w", pid=1, agent_id="agent-worker@adolin")
        assert store.list_agents(include_stale=True) == []

    def test_a_peer_cannot_advertise_an_already_qualified_id(self, store: StateStore) -> None:
        bad = Agent.model_validate(
            {
                "agent_id": "agent-x@thirdhost",
                "name": "x",
                "working_dir": "/x",
                "pid": 1,
                "pid_start": None,
                "last_seen": "2026-01-01T00:00:00Z",
            }
        )
        with pytest.raises(ValueError):
            store.sync_remote_agents("adolin", [bad], owner_pid=1, owner_pid_start=None)


class TestAnIdCannotEscapeTheStateRoot:
    """Reproduction 2: '../..' in an id read and wrote outside the state root."""

    def test_recv_refuses_traversal(self, store: StateStore, tmp_path: Path) -> None:
        # The victim is a VALID message line on purpose. With junk in it, the
        # unvalidated code raised a parse error -- itself a ValueError -- so this
        # test passed with the traversal still open. Mutation-checked.
        victim = Message.model_validate(
            {
                "msg_id": "secret",
                "from_agent": "a",
                "to_agent": "b",
                "body": "CONTENTS OF A FILE OUTSIDE THE STATE ROOT",
                "ts": "2026-01-01T00:00:00Z",
                "in_reply_to": None,
            }
        )
        (tmp_path / "victim.jsonl").write_text(victim.model_dump_json() + "\n")
        with pytest.raises(ValueError, match="not legal"):
            store.recv_messages("../../victim")

    def test_append_refuses_traversal_and_writes_nothing(
        self, store: StateStore, tmp_path: Path
    ) -> None:
        msg = Message.model_validate(
            {
                "msg_id": "m1",
                "from_agent": "agent-peer@adolin",
                "to_agent": "../../pwned-by-peer",
                "body": "x",
                "ts": "2026-01-01T00:00:00Z",
                "in_reply_to": None,
            }
        )
        with pytest.raises(ValueError):
            store.append_message(msg)
        assert list(tmp_path.rglob("pwned-by-peer*")) == []


class TestAHexNameCannotSilentlyTakeOverButUpsertStays:
    """Reproduction 3 is NOT closed by validation, and this pins that it isn't.

    A hex-shaped override (`SPANREED_AGENT_NAME=dac2c1bd`) mints an id in the
    digest namespace, and the colliding register upserts. The owner ruled on #16
    that bus senders are trusted, so the upsert is kept on purpose -- it is what
    makes `set_name`/`set_focus` survive the SessionStart hook. Validation is
    about charset and paths, not collisions; this test exists so nobody reads
    this PR as having fixed the takeover.
    """

    def test_a_legal_colliding_id_is_still_upserted(self, store: StateStore) -> None:
        store.register_agent(name="coordinator", working_dir="/u", pid=1, agent_id="agent-dac2c1bd")
        refreshed = store.register_agent(
            name="other", working_dir="/o", pid=2, agent_id="agent-dac2c1bd"
        )
        assert refreshed.name == "coordinator"
        assert len(store.list_agents(include_stale=True)) == 1
