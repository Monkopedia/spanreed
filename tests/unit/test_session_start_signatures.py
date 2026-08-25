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

from spanreed.cli import _SESSION_START_CONTEXT_TEMPLATE  # pyright: ignore[reportPrivateUsage]
from spanreed.mcp_server import mcp_app

# "  - send_message(from_agent, to_agent, body, in_reply_to?)   — post to ..."
_SIGNATURE = re.compile(r"^\s*-\s+(\w+)\(([^)]*)\)", re.MULTILINE)


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


async def test_the_context_documents_tools_that_exist() -> None:
    documented = _documented()
    assert documented, "parsed nothing — the signature format changed, fix the regex"
    unknown = set(documented) - set(await _actual())
    assert not unknown, f"context advertises tools the server does not expose: {sorted(unknown)}"


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


@pytest.mark.parametrize("tool", ["send_message", "recv_messages", "wait_for_reply"])
async def test_the_three_that_were_wrong(tool: str) -> None:
    """Named individually so the regression is legible in the test output rather
    than only in this file's history."""
    doc_req, _ = _documented()[tool]
    real_req, _ = (await _actual())[tool]
    assert doc_req == real_req
