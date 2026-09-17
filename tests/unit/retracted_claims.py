"""The claims this project has retracted, as data.

A file of its own, holding nothing but the phrase list, because the guard that
scans for these has to scan the file it lives in too -- and a blacklist is
made of the very strings it forbids, so the guard's own source is the one
place it cannot read. Keeping the list here means the exclusion is twenty
lines of data rather than the whole fifteen-hundred-line test module: a false
claim written in `test_codex_worker.py` is still caught.

The residual, stated rather than hidden: a retracted claim asserted *in this
file*, outside the tuple, would not be caught by anything.
"""

from __future__ import annotations

RETRACTED_IN_PROSE = (
    "entire blast radius",
    "the only bound on what",
    "declines approvals for paths outside",
    "declines every approval",
    # The #61 round-1 instance, in its own words.
    "every category app-server will route to a client should reach the operator",
    # Round 2: this survived in `codex_client.py`'s ValueError and in
    # `open-questions.md`, both outside the population the guard then read.
    "bounds everything the worker may touch",
    # Round 3: this survived in a class docstring in `test_codex_worker_faults.py`,
    # in a file the commit titled "retract the boundary claim where it is still
    # asserted" had edited fifty lines below. Nothing scanned `tests/` at all.
    "security boundary",
)
"""Phrases that assert something this project has measured or reasoned to be false."""


FORBIDDEN_IN_EMITTED_STRINGS = (
    "declined outside it",
    "granted inside it",
    # Both articles: "the" was blacklisted, "this" shipped in cli.py.
    "only bound on what the worker may touch",
    "only bound on what this worker may touch",
    "whole blast radius",
    "declines approvals for paths outside",
    "declines every approval",
    "buys you nothing",
)
"""Phrasings already shipped in emitted strings. A regression ratchet, not a
decision procedure -- see :class:`TestNoBoundaryClaimInAnyEmittedString`."""
