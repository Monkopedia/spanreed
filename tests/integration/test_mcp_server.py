"""Integration tests for the MCP server tools.

We call the tool functions directly (the @mcp_app.tool() decorator leaves them
callable) instead of going through the stdio transport. That tests the tool
wiring + StateStore integration without re-testing the MCP library itself.
State root is per-test via the ``SPANREED_STATE_ROOT`` env var.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import anyio
import pytest

from spanreed.mcp_server import (
    deregister_agent,
    list_agents,
    mcp_app,
    recv_messages,
    register_agent,
    request_focus_update,
    send_message,
    set_focus,
    set_name,
    set_status,
    wait_for_reply,
)


@pytest.fixture
def mcp_env(monkeypatch: pytest.MonkeyPatch, state_root: Path) -> Iterator[None]:
    """Bind the MCP server tools to a per-test state root."""
    monkeypatch.setenv("SPANREED_STATE_ROOT", str(state_root))
    yield


@pytest.fixture
def ab(mcp_env: None) -> None:
    """Register toy recipients A and B; send_message rejects unknown recipients."""
    register_agent(name="A", working_dir="/tmp/a", pid=os.getpid(), agent_id="A")
    register_agent(name="B", working_dir="/tmp/b", pid=os.getpid(), agent_id="B")


def test_register_then_list(mcp_env: None) -> None:
    agent = register_agent(name="alice", working_dir="/tmp/x", pid=os.getpid())
    assert agent["name"] == "alice"
    agents = list_agents()
    assert any(a["agent_id"] == agent["agent_id"] for a in agents)


def test_register_with_explicit_id_upserts(mcp_env: None) -> None:
    first = register_agent(
        name="alice-old",
        working_dir="/tmp/x",
        pid=os.getpid(),
        agent_id="agent-fixed",
    )
    second = register_agent(
        name="alice-new",
        working_dir="/tmp/y",
        pid=os.getpid(),
        agent_id="agent-fixed",
    )
    assert second["agent_id"] == first["agent_id"]
    matching = [a for a in list_agents() if a["agent_id"] == "agent-fixed"]
    assert len(matching) == 1
    # Re-register preserves name/working_dir; only pid + last_seen refresh.
    assert matching[0]["name"] == "alice-old"
    assert matching[0]["working_dir"] == "/tmp/x"


def test_deregister_returns_ok(mcp_env: None) -> None:
    agent = register_agent(name="alice", working_dir="/tmp/x", pid=os.getpid())
    result = deregister_agent(agent_id=str(agent["agent_id"]))
    assert result == {"ok": True}
    assert not any(a["agent_id"] == agent["agent_id"] for a in list_agents())


def test_send_and_recv_round_trip(ab: None) -> None:
    msg = send_message(from_agent="A", to_agent="B", body="hello")
    assert msg["body"] == "hello"
    delivered = recv_messages(agent_id="B")
    assert len(delivered) == 1
    assert delivered[0]["msg_id"] == msg["msg_id"]


def test_send_to_unknown_recipient_raises(mcp_env: None) -> None:
    # The tool surfaces the error to the caller instead of silently dropping it.
    with pytest.raises(ValueError, match="not a registered"):
        send_message(from_agent="A", to_agent="nobody", body="hello")


def test_send_resolves_display_name(mcp_env: None) -> None:
    register_agent(name="ksrpc", working_dir="/tmp/k", pid=os.getpid(), agent_id="agent-k")
    msg = send_message(from_agent="A", to_agent="ksrpc", body="hi")
    assert msg["to_agent"] == "agent-k"
    assert len(recv_messages(agent_id="agent-k")) == 1


def test_recv_with_cursor(ab: None) -> None:
    m1 = send_message(from_agent="A", to_agent="B", body="one")
    m2 = send_message(from_agent="A", to_agent="B", body="two")
    after = recv_messages(agent_id="B", since_msg_id=str(m1["msg_id"]))
    assert len(after) == 1
    assert after[0]["msg_id"] == m2["msg_id"]


def test_send_with_in_reply_to(ab: None) -> None:
    m1 = send_message(from_agent="A", to_agent="B", body="ping")
    m2 = send_message(
        from_agent="B",
        to_agent="A",
        body="pong",
        in_reply_to=str(m1["msg_id"]),
    )
    assert m2["in_reply_to"] == m1["msg_id"]


async def test_wait_for_reply_returns_reply(ab: None) -> None:
    m1 = send_message(from_agent="A", to_agent="B", body="ping")

    async def post_reply() -> None:
        await anyio.sleep(0.1)
        send_message(
            from_agent="B",
            to_agent="A",
            body="pong",
            in_reply_to=str(m1["msg_id"]),
        )

    reply: dict[str, object] | None = None
    async with anyio.create_task_group() as tg:
        tg.start_soon(post_reply)
        reply = await wait_for_reply(agent_id="A", in_reply_to=str(m1["msg_id"]), timeout_s=2.0)

    assert reply is not None
    assert reply["body"] == "pong"
    assert reply["in_reply_to"] == m1["msg_id"]


async def test_wait_for_reply_times_out(ab: None) -> None:
    m1 = send_message(from_agent="A", to_agent="B", body="ping")
    reply = await wait_for_reply(agent_id="A", in_reply_to=str(m1["msg_id"]), timeout_s=0.2)
    assert reply is None


async def test_wait_for_reply_returns_a_reply_that_already_landed(ab: None) -> None:
    r"""#14. The semantics an agent most needs, and the ones its docs got backwards.

    A reply arriving between the caller's ``send_message`` and its
    ``wait_for_reply`` is the COMMON case against a fast peer, not an edge one.
    Returning it is deliberate; skipping it silently lost those replies.

    Pinned at the MCP layer as well as in ``store.py``'s tests, and the reason
    is NOT that this defends against the docs drifting — an earlier version of
    this docstring said that and it is false. #14's behaviour was correct all
    along and both layers were green throughout; only the *description* was
    wrong, and the description guard below is what defends that.

    What this earns instead, verified by mutation rather than assumed: the
    wrapper forwards three **positional** arguments through
    ``to_thread.run_sync`` and reshapes the result. Swap ``agent_id`` and
    ``in_reply_to``, or return the raw ``Message`` instead of
    ``model_dump``\ ing it, and every test in ``test_store.py`` stays green
    while this one fails. It pins a contract the store suite structurally
    cannot see.
    """
    m1 = send_message(from_agent="A", to_agent="B", body="ping")
    send_message(from_agent="B", to_agent="A", body="pong", in_reply_to=str(m1["msg_id"]))

    # No task group, no sleep: the reply is already sitting in the inbox.
    reply = await wait_for_reply(agent_id="A", in_reply_to=str(m1["msg_id"]), timeout_s=0.2)

    assert reply is not None, (
        "a reply that landed before the call was dropped — this is the footgun "
        "the current behaviour exists to prevent"
    )
    assert reply["body"] == "pong"


async def test_the_published_description_does_not_contradict_that() -> None:
    """The description is what agents read; the true version lived in store.py.

    Deliberately a heuristic, and worth saying so: this greps published prose
    for the specific false claim (#14 said pre-existing matches "are ignored"),
    so a reworded lie would pass. It is here because the failure being guarded
    is *staleness after a behaviour change*, which reliably leaves the old
    words in place — not adversarial rewording. The assertion above is the
    durable one; this catches the description drifting away from it.
    """
    tool = next(t for t in await mcp_app.list_tools() if t.name == "wait_for_reply")
    description = (tool.description or "").lower()

    # Phrases, not words. `assert "all" in description` was the first version of
    # the positive half and it could never fail: "call", "caller" and "stalling"
    # all satisfy it, and the last is in this very docstring. A guard that cannot
    # fail is worse than none — it reports coverage it does not have.
    # Each phrase must be long enough that only the FALSE claim can contain it.
    # "are ignored" was the first list's entry and it is too generic: a fully
    # truthful sentence — "messages whose ``in_reply_to`` does not match are
    # ignored" — tripped it. That is the false-red direction again, on the guard
    # whose commit message called false-reds the dangerous kind. Third time this
    # assertion has been wrong; the lesson is that a fragment short enough to
    # match a lie robustly is also short enough to match a truth.
    for lie in (
        "pre-existing matching messages are ignored",
        "only considers messages that arrive",
        "arrive *after* this call",
    ):
        assert lie not in description, (
            f"the published description contains {lie!r}, which is the #14 claim: "
            f"pre-existing matches are RETURNED (see the test above). This text is "
            f"loaded into every agent's context."
        )
    # There is deliberately NO positive assertion, and this is the third form of
    # this guard rather than the first. `"all" in description` could not fail.
    # `"pre-existing" in description` could, but on the WRONG input: it reddened a
    # truthful rewrite ("Every message in the inbox counts, including one already
    # present"), which is the failure that teaches the next person to loosen the
    # guard — and the loosened form is the one that cannot fail.
    #
    # Requiring prose to contain a phrase always false-reds on a legitimate
    # rewording, because there are many true ways to say a thing and one list of
    # them is not it. So this guard only ever asserts the ABSENCE of specific
    # known-false claims. That is a real limit, not an oversight: a lie phrased a
    # new way passes. The behaviour test above is what actually pins the
    # semantics; this catches the description going stale after a change, which
    # is the failure that reliably leaves the old words in place.


# ----------------------------------------------------------------- focus


def test_set_focus_on_self(mcp_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """set_focus uses the derived identity of the calling session."""
    monkeypatch.setenv("SPANREED_AGENT_NAME", "alice")
    register_agent(name="alice", working_dir=str(tmp_path), pid=os.getpid(), agent_id="agent-alice")
    result = set_focus(focus="working on the auth refactor")
    assert result is not None
    assert result["focus"] == "working on the auth refactor"


def test_set_focus_appears_in_list_agents(
    mcp_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPANREED_AGENT_NAME", "alice")
    register_agent(name="alice", working_dir=str(tmp_path), pid=os.getpid(), agent_id="agent-alice")
    set_focus(focus="the focus")
    agents = list_agents()
    matching = [a for a in agents if a["agent_id"] == "agent-alice"]
    assert matching[0]["focus"] == "the focus"


def test_set_focus_returns_none_when_not_registered(
    mcp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPANREED_AGENT_NAME", "unregistered")
    assert set_focus(focus="something") is None


# ----------------------------------------------------------------- status


def test_set_status_on_self(mcp_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("SPANREED_AGENT_NAME", "alice")
    register_agent(name="alice", working_dir=str(tmp_path), pid=os.getpid(), agent_id="agent-alice")
    result = set_status(status="blocked")
    assert result is not None
    assert result["status"] == "blocked"


def test_set_status_appears_in_list_agents(
    mcp_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPANREED_AGENT_NAME", "alice")
    register_agent(name="alice", working_dir=str(tmp_path), pid=os.getpid(), agent_id="agent-alice")
    set_status(status="needs_input")
    matching = [a for a in list_agents() if a["agent_id"] == "agent-alice"]
    assert matching[0]["status"] == "needs_input"


def test_set_status_returns_none_when_not_registered(
    mcp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPANREED_AGENT_NAME", "unregistered")
    assert set_status(status="working") is None


async def test_request_focus_update_returns_reply_body(
    mcp_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Simulate the recipient replying to the focus-update request."""
    monkeypatch.setenv("SPANREED_AGENT_NAME", "alice")
    register_agent(name="alice", working_dir=str(tmp_path), pid=os.getpid(), agent_id="agent-alice")
    register_agent(name="bob", working_dir=str(tmp_path), pid=os.getpid(), agent_id="agent-bob")

    async def respond() -> None:
        # Wait for the request to land in bob's inbox, then reply to alice.
        await anyio.sleep(0.1)
        msgs = recv_messages(agent_id="agent-bob")
        # Find the focus-update request (most recent).
        request = msgs[-1]
        send_message(
            from_agent="agent-bob",
            to_agent="agent-alice",
            body="refactoring the API gateway",
            in_reply_to=str(request["msg_id"]),
        )

    result_holder: list[str | None] = []

    async with anyio.create_task_group() as tg:
        tg.start_soon(respond)
        result_holder.append(await request_focus_update(agent_id="agent-bob", timeout_s=2.0))

    assert result_holder == ["refactoring the API gateway"]


async def test_request_focus_update_times_out(
    mcp_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPANREED_AGENT_NAME", "alice")
    register_agent(name="alice", working_dir=str(tmp_path), pid=os.getpid(), agent_id="agent-alice")
    register_agent(name="bob", working_dir=str(tmp_path), pid=os.getpid(), agent_id="agent-bob")
    result = await request_focus_update(agent_id="agent-bob", timeout_s=0.2)
    assert result is None


# ----------------------------------------------------------------- name


def test_set_name_renames_self(
    mcp_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPANREED_AGENT_NAME", "alice")
    register_agent(name="alice", working_dir=str(tmp_path), pid=os.getpid(), agent_id="agent-alice")
    result = set_name(name="main-coordinator")
    assert result is not None
    assert result["name"] == "main-coordinator"


def test_set_name_returns_none_when_not_registered(
    mcp_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPANREED_AGENT_NAME", "unregistered")
    assert set_name(name="anything") is None
