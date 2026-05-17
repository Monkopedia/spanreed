.PHONY: install install-dev uninstall test lint fmt fmt-check check clean experiment-monitor experiment-cross-session help

UV := uv
PYTHON := $(UV) run python

help:
	@echo "Common targets:"
	@echo "  make install         - install spanreed-bus + dev deps (editable)"
	@echo "  make install-plugin  - load the spanreed plugin into Claude Code (user scope)"
	@echo "  make uninstall       - remove installed spanreed-bus"
	@echo "  make test            - run pytest"
	@echo "  make lint            - run ruff check + pyright"
	@echo "  make fmt             - run ruff format + ruff check --fix"
	@echo "  make fmt-check       - verify formatting without modifying"
	@echo "  make check           - fmt-check + lint + test (CI parity)"
	@echo "  make clean           - remove build/cache artifacts"
	@echo "  make experiment-monitor          - launch the bus-test plugin"
	@echo "  make experiment-cross-session    - print instructions for the cross-session test"

install:
	$(UV) sync

install-plugin:
	@echo "Loading plugin from $(CURDIR)/plugins/spanreed into Claude Code (user scope)..."
	claude /plugin install $(CURDIR)/plugins/spanreed --scope user

uninstall:
	$(UV) tool uninstall spanreed-bus 2>/dev/null || true

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check
	$(UV) run pyright

fmt:
	$(UV) run ruff format
	$(UV) run ruff check --fix

fmt-check:
	$(UV) run ruff format --check
	$(UV) run ruff check

check: fmt-check lint test

clean:
	rm -rf build/ dist/ .pytest_cache/ .ruff_cache/ .pyright/ src/*.egg-info/
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

experiment-monitor:
	claude --plugin-dir $(CURDIR)/experiments/bus-test

experiment-cross-session:
	@echo "Cross-session test requires two terminals. In two separate terminals, run:"
	@echo ""
	@echo "  rm -rf /tmp/spanreed-test  # reset state once before either session starts"
	@echo "  claude --plugin-dir $(CURDIR)/experiments/cross-session/spanreed-alice"
	@echo "  claude --plugin-dir $(CURDIR)/experiments/cross-session/spanreed-bob"
	@echo ""
	@echo "Then in alice's terminal, try: 'Send a message to bob asking what TS version they use.'"
