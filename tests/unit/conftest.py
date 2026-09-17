"""Fixtures shared by the Codex worker tests.

``worker_cwd`` and ``make_worker`` live here rather than in
``test_codex_worker.py`` because two modules now use them — the behaviour tests
and the fault-injection tests — and a fixture imported across test modules is
a fixture pytest resolves twice under one name. One definition, discovered the
way pytest discovers fixtures, keeps both modules honest about running against
the *same* worker wiring.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spanreed.codex_client import CodexClient
from spanreed.codex_worker import CodexWorker, WorkerConfig
from spanreed.store import StateStore
from tests.unit.test_codex_client import Handler, StubServer
from tests.unit.test_codex_worker import FakeTerminal, MakeWorker, app_server


@pytest.fixture
def worker_cwd(monkeypatch: pytest.MonkeyPatch, state_root: Path, tmp_path: Path) -> Path:
    """Per-test state root, a private CODEX_HOME, and the worker's --cwd."""
    monkeypatch.setenv("SPANREED_STATE_ROOT", str(state_root))
    # Never the developer's real ~/.codex: the auth tests read this path.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    work = tmp_path / "repo"
    work.mkdir()
    return work


@pytest.fixture
def make_worker(tmp_path: Path, store: StateStore, worker_cwd: Path) -> Iterator[MakeWorker]:
    """Build a worker wired to a fresh stub app-server, started by default."""
    workers: list[CodexWorker] = []
    stubs: list[StubServer] = []
    counter = [0]

    def make(
        handler: Handler | None = None, *, start: bool = True, **config_kw: Any
    ) -> tuple[StubServer, CodexWorker]:
        counter[0] += 1
        # The ask-mode terminal. Split out of config_kw because these are
        # CodexWorker arguments, not WorkerConfig fields, and defaulted here so
        # that every ask-mode test does not have to remember: a worker built
        # with the REAL stdin would either refuse to start (no TTY under pytest)
        # or block the suite forever on the first approval. A test that wants
        # either of those passes its own stream.
        prompt_in = config_kw.pop("prompt_in", None)
        prompt_out = config_kw.pop("prompt_out", None)
        if config_kw.get("mode") == "ask" and prompt_in is None:
            prompt_in = FakeTerminal()  # a terminal that answers nothing: EOF
        stub = StubServer(
            tmp_path / f"worker-stub-{counter[0]}.sock",
            handler if handler is not None else app_server(),
        )
        stubs.append(stub)
        config_kw.setdefault("name", "reviewer")
        # `cwd` is overridable so a test can hand the worker a symlink or a
        # directory it is about to delete; everything else defaults to the
        # per-test --cwd.
        config_kw.setdefault("cwd", worker_cwd)
        config = WorkerConfig(**config_kw)

        def factory(on_request: Any, on_notification: Any) -> CodexClient:
            return CodexClient(
                socket_path=stub.path,
                timeout=5.0,
                turn_timeout=5.0,
                on_server_request=on_request,
                on_notification=on_notification,
            )

        worker = CodexWorker(
            config,
            store=store,
            client_factory=factory,
            log_stream=None,
            poll_interval=0.3,
            prompt_in=prompt_in,
            prompt_out=prompt_out if prompt_out is not None else io.StringIO(),
        )
        workers.append(worker)
        if start:
            worker.start()
        return stub, worker

    yield make
    for worker in workers:
        worker.close()
    for stub in stubs:
        stub.close()
