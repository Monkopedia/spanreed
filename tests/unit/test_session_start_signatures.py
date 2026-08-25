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
"""

from __future__ import annotations

import re

import pytest

from spanreed.cli import (
    _DISPOSITION_POLICY,  # pyright: ignore[reportPrivateUsage]
    _SESSION_START_CONTEXT_TEMPLATE,  # pyright: ignore[reportPrivateUsage]
)
from spanreed.mcp_server import mcp_app

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
        "send_message",
        "recv_messages",
        "wait_for_reply",
        "set_focus",
        "set_name",
        "request_focus_update",
    }
)


def _documented() -> dict[str, tuple[set[str], set[str]]]:
    """``{tool: (required, optional)}`` as the injected context advertises it.

    A trailing ``?`` marks optional, which is the notation the context already
    used; parsing it rather than inventing one keeps the prose readable to the
    agent that has to follow it.
    """
    out: dict[str, tuple[set[str], set[str]]] = {}
    for name, params in _SIGNATURE.findall(_SESSION_START_CONTEXT_TEMPLATE):
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


def test_the_parse_saw_every_tool_it_should_have() -> None:
    """Guards the parser, not the content — the failure that hides all the rest.

    ``assert documented`` catches only a TOTAL parse failure. A partial one is
    silent: reformatting a single bullet drops that tool from the parse and
    every signature assertion below still passes, because they only check what
    they managed to find.
    """
    assert set(_documented()) == _DOCUMENTED_TOOLS, (
        "the parse does not match the tools the context is expected to advertise. "
        "Either a tool was added/removed from the context (update _DOCUMENTED_TOOLS) "
        "or a bullet was reformatted so the regex no longer matches it (fix the "
        "formatting, or the regex)."
    )


async def test_the_context_documents_tools_that_exist() -> None:
    unknown = set(_documented()) - set(await _actual())
    assert not unknown, f"context advertises tools the server does not expose: {sorted(unknown)}"


async def test_the_disposition_policy_names_real_parameters() -> None:
    """The policy is injected alongside the tool list and said ``since``.

    ``recv_messages``'s cursor parameter is ``since_msg_id``; the policy told
    agents to "pass the `since` cursor", which is not a parameter of anything.
    It was invisible to the tests above because they parse the raw template,
    where the policy is still a ``{disposition_policy}`` placeholder — so the
    text an agent actually reads was never scanned.
    """
    _, optional = (await _actual())["recv_messages"]
    cursors = {p for p in optional if "since" in p}
    assert cursors, "recv_messages no longer has a since-style cursor; update this test"
    for name in cursors:
        assert name in _DISPOSITION_POLICY or "cursor" not in _DISPOSITION_POLICY, (
            f"the disposition policy references a cursor but not by its real name "
            f"({sorted(cursors)}); an agent following it passes a parameter that "
            f"does not exist"
        )


async def test_every_documented_signature_matches_the_server() -> None:
    """The assertion #32 needed. Checked per tool so a failure names the tool."""
    actual = await _actual()
    for name, (doc_req, doc_opt) in sorted(_documented().items()):
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
async def test_each_documented_tool_individually(tool: str) -> None:
    """Every documented tool, not just the three that were wrong.

    Driven off ``_DOCUMENTED_TOOLS`` so a tool cannot be pinned by nothing: if
    the parse loses one, this raises ``KeyError`` for it by name. The earlier
    revision parametrized only the three known-bad tools, leaving the other
    four unpinned.
    """
    doc_req, _ = _documented()[tool]
    real_req, _ = (await _actual())[tool]
    assert doc_req == real_req
