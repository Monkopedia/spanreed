"""Fixtures shared by the Codex worker tests.

``worker_cwd`` and ``make_worker`` live here rather than in
``test_codex_worker.py`` because two modules now use them — the behaviour tests
and the fault-injection tests — and a fixture imported across test modules is
a fixture pytest resolves twice under one name. One definition, discovered the
way pytest discovers fixtures, keeps both modules honest about running against
the *same* worker wiring.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spanreed.codex_client import CodexClient
from spanreed.codex_worker import CodexWorker, WorkerConfig
from spanreed.store import StateStore
from tests.unit.test_codex_client import Handler, StubServer
from tests.unit.test_codex_worker import MakeWorker, app_server


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
