# Development

## Setup

Requirements: [`uv`](https://github.com/astral-sh/uv), Python 3.12 (uv will install it if missing), and Claude Code for the plugin side.

```bash
git clone git@github.com:Monkopedia/spanreed.git
cd spanreed
make install                  # sync deps into .venv
uv run pre-commit install     # enable pre-commit hooks
```

Optionally load the plugin from your working copy:

```bash
make install-plugin           # claude /plugin install ./plugins/spanreed --scope user
```

## Common tasks

```bash
make test       # pytest
make lint       # ruff check + pyright
make fmt        # ruff format + ruff check --fix
make check      # fmt-check + lint + test (what CI runs)
```

## Running experiments

```bash
make experiment-monitor          # the single-session Monitor probe
make experiment-cross-session    # prints instructions for the two-terminal test
```

The experiments are the original validation scaffolds from the design phase; they're kept in `experiments/` for reference and as the basis for end-to-end regression tests.

## Testing layout

- `tests/unit/`        — isolated logic (protocol types, store I/O against tmp paths).
- `tests/integration/` — spin up the MCP server and exercise tools against it.
- End-to-end with real Claude Code stays manual for now; the experiments serve as the smoke test.

Pytest is configured with `asyncio_mode = "auto"`, so `async def test_*` functions just work.

## CI

GitHub Actions runs `ruff format --check`, `ruff check`, `pyright`, and `pytest` on every push to main and every PR. Local `make check` should match what CI does.

## Conventions

See [`../CLAUDE.md`](../CLAUDE.md) for the project discipline rules — they apply to humans and Claude sessions alike. The short version:

- Docs are authoritative for design. Update them in the same commit as behavior changes.
- Tests required for new code paths.
- One concern per commit.
- The README is tested by running it.
