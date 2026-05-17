# Working in this repo

Project ethos: **correctness over speed**. Claude can make development fast; the human's job is to ensure it stays correct and maintainable. These rules exist to keep that property as the codebase grows.

## Architecture summary

Spanreed is an inter-agent message bus for local Claude Code sessions. Each session runs a plugin (`plugins/spanreed/`) that includes:

- A **SessionStart hook** that injects bus topology into Claude's context.
- A **Monitor** that surfaces inbox notifications as Claude wakes events.
- An **MCP server** (`src/spanreed/mcp_server.py`) providing typed bus tools.

Sessions coordinate through filesystem state under `~/.claude/spanreed/` (registry, per-agent inboxes, per-session cursors). There is no central daemon; each plugin's MCP server is per-session and reads/writes shared files.

`docs/architecture.md` is the authoritative design doc. `docs/protocol.md` is the bus wire-format spec. Always defer to those over inference from code.

## Rules

1. **Docs are authoritative for design.** Behavior changes (especially anything that touches the wire format, the MCP tool surface, or filesystem layout) require updating `docs/architecture.md` or `docs/protocol.md` in the same commit. Don't infer design from code; if the doc is wrong, fix the doc.

2. **Tests required for new code paths.** If you add a function, add a test. If you change behavior, update the test. Run `make test` before commit. Pre-commit gates this.

3. **Findings ≠ design.** Empirical results from running experiments go in `docs/findings.md`. Decisions about how things *should* work go in `docs/architecture.md` or `docs/protocol.md`. Don't mix them.

4. **Open questions get tracked.** If a design decision was deferred, it goes in `docs/open-questions.md` — don't leave TODO comments in code as a substitute.

5. **No silent scope creep.** Don't add features, refactor adjacent code, or introduce abstractions beyond what the task requires. Cleanups belong in their own commits with a description of why.

6. **The README is tested by being run.** If you change install or usage flow, manually re-run the README commands and update them in the same commit. The README is not aspirational; it is the truth.

7. **Visibility over hiding.** For anything user-facing in bus UX (notification text, status indicators, monitor output), prefer verbose and legible over clean and hidden. The user wants to *see* inter-agent reasoning, not abstract it away.

8. **Surface design calls, don't decide alone.** When implementing reveals a design ambiguity not covered by the docs, ask the user before deciding. Then update the docs with the resolution.

## How to run things

```bash
make install      # sync dependencies
make test         # run pytest
make lint         # ruff check + pyright
make fmt          # ruff format + ruff check --fix
make check        # everything (CI parity)
```

For dev install of the plugin: `make install-plugin` (puts the plugin into Claude Code at user scope from the local dir).

## Commit style

- Subject line ≤ 70 chars, present tense, explains intent.
- Body explains the **why**, not the what.
- Co-author trailer for Claude commits (the harness adds this automatically).
- One concern per commit; mixed-concern commits get split.

## Where things live

```
src/spanreed/      Python package (MCP server, store, CLI)
tests/             pytest tests (unit/, integration/)
plugins/spanreed/  the real Claude Code plugin
experiments/       validation scaffolds from the design phase (kept for reference)
docs/              architecture, protocol, findings, open questions
.claude-plugin/    marketplace manifest (this repo doubles as a single-plugin marketplace)
```

## Git workflow

For now: **direct commits to main** with discipline (pre-commit must pass). Once v1 is usable, switch to **PR-based workflow** and dogfood spanreed for cross-session PR review (the project's whole motivating use case).
