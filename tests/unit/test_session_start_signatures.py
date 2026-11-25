"""The SessionStart context must document the tool surface it actually has.

#32: the context injected on every `startup` and `resume` advertised
``send_message(to_agent, body, in_reply_to?)``, ``recv_messages(since?)`` and
``wait_for_reply(in_reply_to, timeout_s)``. All three omit a **required**
parameter, so an agent following the text verbatim fails schema validation on
its first call — and this is the text that teaches every agent how to use the
bus.

212 tests passed with all three wrong. Nothing tied the prose to the server:
``test_smoke`` asserts tool *names* only, and the two context tests are
substring checks. This module is that tie. It derives the truth from
``mcp_app.list_tools()`` rather than restating it, so the next signature change
fails here instead of on a user's machine.

**Parameter ORDER is deliberately not asserted.** Signatures are parsed into
sets, so reordering a documented signature passes. MCP is keyword-addressed, so
order cannot affect whether a call is valid — asserting it would bind this
suite to Python declaration order and turn a no-op refactor red for a reason
unrelated to what these tests protect. Written down because a deliberate
non-assertion that is not recorded is indistinguishable from a missing one, and
the next reader will otherwise "fix" it.

#41: **everything here scans the RENDERED context**, the text an agent actually
receives, in both status-tracking gate states. An earlier revision parsed the
raw template, where the disposition policy is still a ``{disposition_policy}``
placeholder and ``_STATUS_INSTRUCTION`` is absent entirely, because it is
appended only when tracking is on. So the policy's "pass the `since` cursor"
error was never scanned and needed a point assertion of its own, and a
signature line added to the gated block would have shipped unchecked.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from spanreed import cli
from spanreed.cli import (
    _DISPOSITION_POLICY,  # pyright: ignore[reportPrivateUsage]
    _STATUS_INSTRUCTION,  # pyright: ignore[reportPrivateUsage]
)
from spanreed.mcp_server import mcp_app
from spanreed.store import StateStore

# "  - send_message(from_agent, to_agent, body, in_reply_to?)   — post to ..."
_SIGNATURE = re.compile(r"^\s*-\s+(\w+)\(([^)]*)\)", re.MULTILINE)

# The tools the context is expected to advertise. Named explicitly because a
# regex over prose degrades SILENTLY: reformat one bullet and that tool drops
# out of the parse while every assertion below still passes, which is a test
# that looks like coverage and is not — the defect class #32 is about, and one
# an earlier revision of this very file had. Verified: dropping list_agents'
# signature left the suite green at 239 until this constant existed.
_DOCUMENTED_TOOLS = frozenset(
    {
        "list_agents",
        "list_peers",
        "send_message",
        "recv_messages",
        "wait_for_reply",
        "set_focus",
        "set_name",
        "request_focus_update",
    }
)

# Advertised only by _STATUS_INSTRUCTION, so only when status tracking is on.
# Expected in exactly that render: a tracking-on context that lost the line, or
# a tracking-off one that leaked it, fails the parse guard below.
_GATED_TOOLS = frozenset({"set_status"})

# A backticked span, which is how the context presents every parameter and
# value it names in prose: "pass the `since_msg_id` cursor", "set_status
# `working`". Prose words are never backticked, so ordinary English ("arrived
# since your last read") is not mistaken for a parameter.
_BACKTICKED = re.compile(r"`([^`]*)`")

# The parameters the context is expected to name in prose. Pinned for the same
# reason as _DOCUMENTED_TOOLS: the vocabulary check below catches a WRONG name,
# but a parameter that is misspelled without backticks, or dropped from the
# text, is simply absent, and absence passes any check that only inspects what
# it found. since_msg_id is named by the disposition policy, which is also
# written standalone to disposition-policy.md without the tool list beside it,
# so it has to name the cursor itself.
_NAMED_PARAMETERS = frozenset({"since_msg_id"})


@pytest.fixture(params=[False, True], ids=["tracking-off", "tracking-on"])
def rendered(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    state_root: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> str:
    """The context ``session-start`` emits, with status tracking off and on.

    Produced by running the real command rather than formatting the template
    here, so what is scanned is what an agent receives, however that is
    assembled.
    """
    tracking: bool = request.param
    monkeypatch.setenv("SPANREED_STATE_ROOT", str(state_root))
    monkeypatch.delenv("SPANREED_AGENT_NAME", raising=False)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    monkeypatch.chdir(tmp_path)
    StateStore(root=state_root).set_status_tracking(tracking)
    assert cli.main(["session-start"]) == 0
    context: str = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    # Guards the fixture: without these, a render that lost a block would make
    # every test below vacuously green for that block.
    assert _DISPOSITION_POLICY in context
    assert (_STATUS_INSTRUCTION in context) is tracking
    return context


def _documented(context: str) -> dict[str, tuple[set[str], set[str]]]:
    """``{tool: (required, optional)}`` as the injected context advertises it.

    A trailing ``?`` marks optional, which is the notation the context already
    used; parsing it rather than inventing one keeps the prose readable to the
    agent that has to follow it.
    """
    out: dict[str, tuple[set[str], set[str]]] = {}
    for name, params in _SIGNATURE.findall(context):
        req: set[str] = set()
        opt: set[str] = set()
        for raw in (p.strip() for p in params.split(",")):
            if not raw:
                continue
            (opt if raw.endswith("?") else req).add(raw.rstrip("?"))
        out[name] = (req, opt)
    return out


async def _actual() -> dict[str, tuple[set[str], set[str]]]:
    out: dict[str, tuple[set[str], set[str]]] = {}
    for tool in await mcp_app.list_tools():
        schema = tool.input_schema
        required = set(schema.get("required", []))
        out[tool.name] = (required, set(schema.get("properties", {})) - required)
    return out


def test_the_parse_saw_every_tool_it_should_have(rendered: str) -> None:
    """Guards the parser, not the content — the failure that hides all the rest.

    ``assert documented`` catches only a TOTAL parse failure. A partial one is
    silent: reformatting a single bullet drops that tool from the parse and
    every signature assertion below still passes, because they only check what
    they managed to find.
    """
    # The fixture has already pinned _STATUS_INSTRUCTION's presence to the
    # tracking state, so this is the tracking state.
    gated = _GATED_TOOLS if _STATUS_INSTRUCTION in rendered else frozenset[str]()
    assert set(_documented(rendered)) == _DOCUMENTED_TOOLS | gated, (
        "the parse does not match the tools the context is expected to advertise. "
        "Either a tool was added/removed from the context (update _DOCUMENTED_TOOLS "
        "or _GATED_TOOLS) "
        "or a bullet was reformatted so the regex no longer matches it (fix the "
        "formatting, or the regex)."
    )


async def test_the_context_documents_tools_that_exist(rendered: str) -> None:
    unknown = set(_documented(rendered)) - set(await _actual())
    assert not unknown, f"context advertises tools the server does not expose: {sorted(unknown)}"


async def test_every_documented_signature_matches_the_server(rendered: str) -> None:
    """The assertion #32 needed. Checked per tool so a failure names the tool."""
    actual = await _actual()
    for name, (doc_req, doc_opt) in sorted(_documented(rendered).items()):
        real_req, real_opt = actual[name]
        assert doc_req == real_req, (
            f"{name}: context says required={sorted(doc_req)}, "
            f"server requires {sorted(real_req)}. An agent following the context "
            f"fails schema validation."
        )
        assert doc_opt <= real_opt, (
            f"{name}: context offers optional params the server has no parameter for: "
            f"{sorted(doc_opt - real_opt)}"
        )


@pytest.mark.parametrize("tool", sorted(_DOCUMENTED_TOOLS))
async def test_each_documented_tool_individually(rendered: str, tool: str) -> None:
    """Every documented tool, not just the three that were wrong.

    Driven off ``_DOCUMENTED_TOOLS`` so a tool cannot be pinned by nothing: if
    the parse loses one, this raises ``KeyError`` for it by name. The earlier
    revision parametrized only the three known-bad tools, leaving the other
    four unpinned.
    """
    doc_req, _ = _documented(rendered)[tool]
    real_req, _ = (await _actual())[tool]
    assert doc_req == real_req


async def test_every_name_in_backticks_is_real(rendered: str) -> None:
    """A backticked identifier must be a tool, a parameter, or a value the server takes.

    Replaces #40's point assertion on ``since``-prefixed tokens in the policy,
    which caught one misspelling of one parameter and nothing else: a wrong
    name that was not ``since``-prefixed passed it. This derives the vocabulary
    from the server instead, so it covers every tool, every block, and the
    status values in ``_STATUS_INSTRUCTION``, without knowing what any of them
    are. Spans that are not a bare identifier (``spanreed conjoin``, a path) are
    not names of anything on the server and are skipped.
    """
    vocabulary: set[str] = set()
    for tool in await mcp_app.list_tools():
        vocabulary.add(tool.name)
        for name, prop in tool.input_schema.get("properties", {}).items():
            vocabulary.add(name)
            vocabulary.update(prop.get("enum", []))
    named = {span for span in _BACKTICKED.findall(rendered) if re.fullmatch(r"\w+", span)}
    unknown = named - vocabulary
    assert not unknown, (
        f"the context names {sorted(unknown)} in backticks, which no tool takes as a "
        f"parameter or value. An agent following it passes something that does not exist."
    )


async def test_the_context_names_the_parameters_it_should(rendered: str) -> None:
    """The other half of the check above, which only sees names that are present.

    A cursor misspelled without backticks, or a policy that stops mentioning
    it, leaves no wrong name to catch. Pinned by ``_NAMED_PARAMETERS``.
    """
    params = {
        p for tool in await mcp_app.list_tools() for p in tool.input_schema.get("properties", {})
    }
    named = set(_BACKTICKED.findall(rendered)) & params
    assert named == _NAMED_PARAMETERS, (
        f"the context names parameters {sorted(named)} in backticks; expected "
        f"{sorted(_NAMED_PARAMETERS)}. Either a parameter reference was dropped or "
        f"misspelled without backticks (fix the text), or one was added or removed on "
        f"purpose (update _NAMED_PARAMETERS)."
    )
