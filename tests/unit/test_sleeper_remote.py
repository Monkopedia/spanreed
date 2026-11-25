"""The target's side of remote sleeper management (#86, ``sleeper_remote``).

Frames are handed to :class:`Target` directly, the way the bridge's reader and
worker will; no bridge, pipe or second machine is involved. Each security test
names, in its docstring, the check it exists to catch the removal of.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from spanreed import sleeper_ops
from spanreed.sleeper_remote import RECORD_RETENTION, RequestRecord, Target, prune_records
from spanreed.store import StateStore


@pytest.fixture
def tools(tmp_path: Path) -> str:
    """A PATH holding fake ``claude`` and ``spanreed``, and nothing else."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("claude", "spanreed"):
        exe = bin_dir / tool
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)
    return str(bin_dir)


@pytest.fixture
def switched_on(store: StateStore, tools: str) -> StateStore:
    """Remote access on, with ``tools`` as the recorded PATH."""
    store.set_sleeper_remote_access(tools)
    return store


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    return path


def target(store: StateStore, **kw: Any) -> Target:
    return Target(store, self_host="adolin", peer="kaladin", role="serve", **kw)


def frame(
    op: str = "create",
    args: dict[str, Any] | None = None,
    *,
    request_id: str | None = None,
    expires_in: float = 30,
) -> dict[str, Any]:
    expires_at = (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat()
    sent: dict[str, Any] = {
        "kind": "sleeper-op",
        "request_id": request_id or uuid.uuid4().hex,
        "expires_at": expires_at,
        "op": op,
        "args": args if args is not None else {},
        "requested_by": "jmonk on kaladin",
    }
    return sent


def create_args(cwd: str, **overrides: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "name": "nap",
        "cwd": cwd,
        "permission_mode": "plan",
        "model": None,
        "idle_timeout": None,
        "trust": False,
        "claude_args": [],
    }
    args.update(overrides)
    return args


def deliver(t: Target, sent: dict[str, Any]) -> dict[str, Any]:
    """What the bridge would do: reply, or run it on the worker. Returns the result."""
    receipt = t.receive(sent)
    if receipt.action == "run":
        assert receipt.request_id is not None
        result = t.run(receipt.request_id)
        assert result is not None, "a request this process queued must run here"
        return result
    assert receipt.action == "reply", receipt
    assert receipt.result is not None
    return receipt.result


def audit(store: StateStore) -> list[dict[str, Any]]:
    path = store.root / "logs" / "sleeper-ops.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    return proc.pid


# ------------------------------------------------------------------ the switch


class TestSwitch:
    def test_it_is_off_by_default(self, store: StateStore) -> None:
        assert store.get_sleeper_remote_access() is None

    def test_a_request_is_refused_while_it_is_off(self, store: StateStore, proj: Path) -> None:
        """SECURITY. Catches removal of the switch check in ``Target.receive``."""
        result = deliver(target(store), frame("create", create_args(str(proj))))
        assert result["outcome"] == "refused"
        assert result["exit"] == 4
        assert "turned off on adolin" in result["stderr"]
        assert "spanreed sleeper remote-access on" in result["stderr"]
        assert store.sleepers.read("nap") is None
        assert [line["event"] for line in audit(store)] == ["refused"]
        records = list((store.root / "sleeper-ops" / "in").glob("*/*.json"))
        assert records == [], "a refusal records nothing"

    def test_list_is_refused_too(self, store: StateStore) -> None:
        """R2: one gate for everything, reads included."""
        assert deliver(target(store), frame("list"))["outcome"] == "refused"

    @pytest.mark.parametrize(
        "config",
        [
            "not json",
            json.dumps({"sleeper_remote_access": True}),
            json.dumps({"sleeper_remote_access": {"enabled": "true", "path_env": "/bin"}}),
            json.dumps({"sleeper_remote_access": {"enabled": 1, "path_env": "/bin"}}),
        ],
    )
    def test_anything_but_enabled_true_reads_as_off(self, store: StateStore, config: str) -> None:
        (store.root / "config.json").write_text(config)
        assert store.get_sleeper_remote_access() is None

    def test_on_and_off_take_effect_on_the_next_request(
        self, store: StateStore, tools: str
    ) -> None:
        t = target(store)
        assert deliver(t, frame("list"))["outcome"] == "refused"
        store.set_sleeper_remote_access(tools)
        assert deliver(t, frame("list"))["outcome"] == "ok"
        store.set_sleeper_remote_access(None)
        assert deliver(t, frame("list"))["outcome"] == "refused"

    def test_off_keeps_other_config_keys(self, store: StateStore, tools: str) -> None:
        store.set_status_tracking(True)
        store.set_sleeper_remote_access(tools)
        store.set_sleeper_remote_access(None)
        assert json.loads((store.root / "config.json").read_text()) == {"status_tracking": True}

    def test_a_request_queued_when_it_is_turned_off_does_not_start(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        t = target(switched_on)
        receipt = t.receive(frame("create", create_args(str(proj))))
        assert receipt.action == "run" and receipt.request_id
        switched_on.set_sleeper_remote_access(None)
        result = t.run(receipt.request_id)
        assert result is not None and result["outcome"] == "refused"
        assert switched_on.sleepers.read("nap") is None

    def test_a_create_uses_the_recorded_path_not_the_bridges(
        self, switched_on: StateStore, proj: Path, tools: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", "/nonexistent")
        result = deliver(target(switched_on), frame("create", create_args(str(proj))))
        assert result["outcome"] == "ok", result
        record = switched_on.sleepers.read("nap")
        assert record is not None
        assert record.path_env == tools
        assert record.claude_path == str(Path(tools) / "claude")

    def test_a_create_with_no_recorded_path_names_remote_access_on(
        self, store: StateStore, proj: Path
    ) -> None:
        (store.root / "config.json").write_text(
            json.dumps({"sleeper_remote_access": {"enabled": True}})
        )
        result = deliver(target(store), frame("create", create_args(str(proj))))
        assert result["outcome"] == "failed"
        assert "remote-access on" in result["stderr"]


# ------------------------------------------------- a remote create's own checks


class TestRemoteCreateChecks:
    """A remote create gets exactly the local checks, made on the target (``sleeper_ops``)."""

    def _create(self, store: StateStore, args: dict[str, Any]) -> dict[str, Any]:
        result = deliver(target(store), frame("create", args))
        assert store.sleepers.read("nap") is None, "a refused create leaves nothing"
        return result

    def test_the_claude_args_allowlist(self, switched_on: StateStore, proj: Path) -> None:
        """SECURITY. Catches removal of ``check_claude_args`` from ``sleeper_ops.create``."""
        result = self._create(
            switched_on, create_args(str(proj), claude_args=["--allowedTools", "Bash"])
        )
        assert (result["outcome"], result["exit"]) == ("failed", 1)
        assert (
            "'--allowedTools' is not one of the claude flags a sleeper accepts"
            in (result["stderr"])
        )

    @pytest.mark.parametrize("where", ["home", "ancestor"])
    def test_trust_refuses_the_home_folder_and_above(
        self,
        switched_on: StateStore,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        where: str,
    ) -> None:
        """SECURITY. Catches removal of ``refuse_broad_folder`` from ``trust_folder``.

        Claude Code's state file exists and is well formed, so without the
        refusal the grant would be written and the create would succeed.
        """
        home = tmp_path / "home" / "u"
        home.mkdir(parents=True)
        monkeypatch.setenv("HOME", str(home))
        state = Path(os.environ["CLAUDE_CONFIG_DIR"]) / ".claude.json"
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"projects": {}}))
        cwd = "~" if where == "home" else str(home.parent)
        result = self._create(switched_on, create_args(cwd, trust=True))
        assert (result["outcome"], result["exit"]) == ("failed", 1)
        assert "--trust: refusing to trust" in result["stderr"]
        assert json.loads(state.read_text()) == {"projects": {}}, "no trust written"

    def test_a_permission_mode_is_required(self, switched_on: StateStore, proj: Path) -> None:
        """SECURITY. Catches a default on ``CreateArgs.permission_mode``: with no mode
        passed, every launch would take whatever Claude Code's settings say."""
        args = create_args(str(proj))
        del args["permission_mode"]
        result = self._create(switched_on, args)
        assert (result["outcome"], result["exit"]) == ("invalid", 2)
        assert "permission_mode" in result["stderr"]

    def test_the_permission_mode_must_be_a_known_one(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        """SECURITY. Catches removal of the ``PERMISSION_MODES`` check from
        ``sleeper_ops.create``, which argparse alone made before #86."""
        result = self._create(switched_on, create_args(str(proj), permission_mode="yolo"))
        assert (result["outcome"], result["exit"]) == ("failed", 1)
        assert "'yolo' is not one of" in result["stderr"]

    def test_the_cwd_must_be_absolute(
        self, switched_on: StateStore, proj: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SECURITY. Catches removal of the absolute-or-~ check for a remote create.

        ``proj`` exists relative to the process's cwd here, so without the check
        it would resolve against wherever the bridge started, and succeed.
        """
        monkeypatch.chdir(proj.parent)
        result = self._create(switched_on, create_args("proj"))
        assert (result["outcome"], result["exit"]) == ("failed", 1)
        assert "must be absolute or start with ~" in result["stderr"]

    def test_a_name_that_is_not_a_legal_id(self, switched_on: StateStore, proj: Path) -> None:
        result = self._create(switched_on, create_args(str(proj), name="../x"))
        assert result["outcome"] == "failed"
        assert "is not legal" in result["stderr"]

    def test_a_bad_idle_timeout(self, switched_on: StateStore, proj: Path) -> None:
        result = self._create(switched_on, create_args(str(proj), idle_timeout="soon"))
        assert result["outcome"] == "failed"
        assert "not a duration" in result["stderr"]

    def test_a_valid_create_succeeds_with_the_local_output(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        result = deliver(
            target(switched_on), frame("create", create_args(str(proj), idle_timeout="15m"))
        )
        assert (result["outcome"], result["exit"]) == ("ok", 0)
        assert result["stdout"].startswith("created sleeper nap (agent-nap), ASLEEP")
        assert result["records"] is None
        record = switched_on.sleepers.read("nap")
        assert record is not None and record.idle_timeout_s == 900


# ------------------------------------------------------------------ the frame


class TestInvalidFrames:
    def test_a_bad_request_id_gets_no_reply(self, switched_on: StateStore, proj: Path) -> None:
        receipt = target(switched_on).receive(
            frame("create", create_args(str(proj)), request_id="../../x")
        )
        assert receipt.action == "drop"
        assert receipt.note is not None and "invalid request_id" in receipt.note
        assert [line["event"] for line in audit(switched_on)] == ["invalid"]
        assert not (switched_on.root / "sleeper-ops" / "in").exists()

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"op": "explode"}, "'explode' is not one this host's spanreed knows"),
            ({"args": {"name": "nap", "extra": 1}}, "malformed"),
            ({"args": None}, "malformed"),
            ({"expires_at": None}, "expires_at is missing"),
            ({"expires_at": "tomorrow"}, "not an ISO 8601"),
            ({"expires_at": "2026-11-25T18:04:40"}, "not in UTC"),
            ({"expires_at": "2026-11-25T18:04:40+05:00"}, "not in UTC"),
            ({"requested_by": 7}, "requested_by"),
        ],
    )
    def test_a_malformed_frame_is_invalid_and_not_run(
        self, switched_on: StateStore, overrides: dict[str, Any], reason: str
    ) -> None:
        sent = frame("wake", {"name": "nap"})
        sent.update(overrides)
        result = deliver(target(switched_on), sent)
        assert (result["outcome"], result["exit"]) == ("invalid", 2)
        assert reason in result["stderr"]
        assert [line["event"] for line in audit(switched_on)] == ["invalid"]

    def test_a_trust_flag_of_the_wrong_type_is_invalid(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        result = deliver(target(switched_on), frame("create", create_args(str(proj), trust="yes")))
        assert result["outcome"] == "invalid"

    def test_an_expiry_too_far_ahead_is_invalid(self, switched_on: StateStore) -> None:
        """No request lives more than 600 s; 630 s allows for clock skew."""
        assert deliver(target(switched_on), frame("list", expires_in=620))["outcome"] == "ok"
        result = deliver(target(switched_on), frame("list", expires_in=700))
        assert result["outcome"] == "invalid"
        assert "ahead of this host's clock" in result["stderr"]


# ------------------------------------------------------------------ expiry


class TestExpiry:
    def test_an_expired_request_is_not_run(self, switched_on: StateStore, proj: Path) -> None:
        """SECURITY. Catches removal of the expiry check on arrival in ``Target.receive``."""
        sent = frame("create", create_args(str(proj)), expires_in=-1)
        result = deliver(target(switched_on), sent)
        assert (result["outcome"], result["exit"]) == ("expired", 2)
        assert "was not run" in result["stderr"]
        assert switched_on.sleepers.read("nap") is None
        assert [line["event"] for line in audit(switched_on)] == ["expired"]
        # Recorded, so a resend is answered from the record and never runs.
        assert deliver(target(switched_on), sent)["outcome"] == "expired"
        assert switched_on.sleepers.read("nap") is None

    def test_a_request_that_expires_while_queued_is_not_run(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        """SECURITY. Catches removal of the expiry re-check in ``Target.run`` (R9)."""
        now = [datetime.now(UTC)]
        t = target(switched_on, clock=lambda: now[0])
        receipt = t.receive(frame("create", create_args(str(proj)), expires_in=30))
        assert receipt.action == "run" and receipt.request_id
        now[0] += timedelta(seconds=31)  # it waited behind another operation
        result = t.run(receipt.request_id)
        assert result is not None and result["outcome"] == "expired"
        assert switched_on.sleepers.read("nap") is None
        assert [line["event"] for line in audit(switched_on)] == ["received", "expired"]


# ------------------------------------------------------------------ idempotency


class TestIdempotency:
    def test_a_duplicate_request_id_is_not_run_twice(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        """SECURITY. Catches removal of the idempotency record lookup in ``Target.receive``.

        A create run twice would also fail its second time ("already exists")
        and report failure for something that worked.
        """
        sent = frame("create", create_args(str(proj)))
        first = deliver(target(switched_on), sent)
        assert first["outcome"] == "ok"
        receipt = target(switched_on).receive(sent)
        assert receipt.action == "reply"
        assert receipt.result == first
        events = [line["event"] for line in audit(switched_on)]
        assert events == ["received", "done", "duplicate"]

    def test_a_resend_that_races_the_first_is_not_run_twice(
        self, switched_on: StateStore, proj: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SECURITY. Catches the record being created by an overwrite rather than
        create-only: two bridge processes for one peer can both find no record
        and both try to write one. The loser must defer to the winner's."""
        sent = frame("create", create_args(str(proj)))
        first = target(switched_on)
        assert first.receive(sent).action == "run"
        racer = target(switched_on, bridge_pid=os.getppid(), bridge_pid_start=None)
        real_read = racer._read  # pyright: ignore[reportPrivateUsage]
        calls: list[str] = []

        def read_missing_it_once(request_id: str) -> RequestRecord | None:
            calls.append(request_id)
            return None if len(calls) == 1 else real_read(request_id)

        # Its first look finds nothing, as if the first holder had not written yet.
        monkeypatch.setattr(racer, "_read", read_missing_it_once)
        assert racer.receive(sent).action == "wait", "the first holder is alive and has it"

    def test_a_duplicate_is_answered_from_the_record_even_once_switched_off(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        """Answering ``refused`` would report a create that ran as never allowed."""
        sent = frame("create", create_args(str(proj)))
        first = deliver(target(switched_on), sent)
        switched_on.set_sleeper_remote_access(None)
        assert deliver(target(switched_on), sent) == first

    def test_a_duplicate_of_one_still_queued_in_a_live_process_waits(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        sent = frame("create", create_args(str(proj)))
        t = target(switched_on)
        first = t.receive(sent)
        assert first.action == "run" and first.request_id
        assert t.receive(sent).action == "wait"
        assert t.result_if_done(first.request_id) is None
        result = t.run(first.request_id)
        assert result is not None
        assert t.result_if_done(first.request_id) == result

    def test_one_running_in_a_dead_process_is_interrupted_not_rerun(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        """SECURITY (R8). Catches a dead ``running`` record being queued again."""
        sent = frame("create", create_args(str(proj)))
        dead = target(switched_on, bridge_pid=dead_pid(), bridge_pid_start=None)
        receipt = dead.receive(sent)
        assert receipt.request_id is not None
        path = switched_on.root / "sleeper-ops" / "in" / "kaladin" / f"{receipt.request_id}.json"
        record = RequestRecord.model_validate_json(path.read_text())
        path.write_text(record.model_copy(update={"state": "running"}).model_dump_json())

        result = deliver(target(switched_on), sent)
        assert (result["outcome"], result["exit"]) == ("interrupted", 1)
        assert "its effect may be partial" in result["stderr"]
        assert "spanreed sleeper list --host adolin" in result["stderr"]
        assert switched_on.sleepers.read("nap") is None
        assert audit(switched_on)[-1]["event"] == "interrupted"
        assert deliver(target(switched_on), sent) == result, "and stays interrupted"

    def test_a_requeue_racing_another_bridge_process_runs_once(
        self, switched_on: StateStore, proj: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SECURITY. Catches the requeue in ``Target._decide`` not being atomic with
        its read: with A and B conjoined both ways, B has two bridge processes for
        A, and both can find a request queued under a dead one and take it over.

        The second bridge process looks at the record while the first has read
        it but not yet re-owned it. It may run only once, between them."""
        sent = frame("create", create_args(str(proj)))
        target(switched_on, bridge_pid=dead_pid(), bridge_pid_start=None).receive(sent)
        first = target(switched_on)
        second = target(switched_on, bridge_pid=os.getppid(), bridge_pid_start=None)
        real_read = first._read  # pyright: ignore[reportPrivateUsage]
        receipts: list[str] = []
        threads: list[threading.Thread] = []

        def read_then_let_the_other_look(request_id: str) -> RequestRecord | None:
            record = real_read(request_id)
            # The other process's reader gets its turn now. If the lookup and
            # the requeue are one locked step, it waits for ours (the join
            # times out) and finds the request held by a live process.
            other = threading.Thread(target=lambda: receipts.append(second.receive(sent).action))
            other.start()
            other.join(timeout=0.5)
            threads.append(other)
            monkeypatch.setattr(first, "_read", real_read)
            return record

        monkeypatch.setattr(first, "_read", read_then_let_the_other_look)
        receipts.append(first.receive(sent).action)
        for thread in threads:
            thread.join(timeout=10)
        assert sorted(receipts) == ["run", "wait"]

    def test_a_running_request_is_not_run_again_by_a_second_worker(
        self, switched_on: StateStore, proj: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SECURITY. Catches ``Target.run`` starting a request that is not still
        ``queued``, or checking that and marking it ``running`` in two steps.

        A second worker in this process starts on the same request while the
        first is between reading it and marking it running, and again while
        the create is under way. The create runs once."""
        sent = frame("create", create_args(str(proj)))
        receipt = target(switched_on).receive(sent)
        assert receipt.action == "run" and receipt.request_id is not None
        request_id = receipt.request_id
        first, second = target(switched_on), target(switched_on)
        real_create = sleeper_ops.create
        runs: list[str] = []
        others: list[dict[str, Any] | None] = []
        threads: list[threading.Thread] = []

        def counting_create(*args: Any, **kw: Any) -> int:
            runs.append(request_id)
            if len(runs) == 1:
                others.append(second.run(request_id))  # mid-create: the record is running
            return real_create(*args, **kw)

        monkeypatch.setattr(sleeper_ops, "create", counting_create)
        real_read = first._read  # pyright: ignore[reportPrivateUsage]

        def read_then_let_the_other_run(rid: str) -> RequestRecord | None:
            record = real_read(rid)
            monkeypatch.setattr(first, "_read", real_read)
            other = threading.Thread(target=lambda: others.append(second.run(rid)))
            other.start()
            other.join(timeout=0.5)
            threads.append(other)
            return record

        monkeypatch.setattr(first, "_read", read_then_let_the_other_run)
        result = first.run(request_id)
        for thread in threads:
            thread.join(timeout=10)
        assert result is not None and result["outcome"] == "ok"
        assert len(runs) == 1, f"the create ran {len(runs)} times"
        assert others == [None, None]

    def test_run_refuses_a_request_queued_in_another_live_process(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        """SECURITY. Catches ``Target.run`` not checking the record is queued in
        *this* bridge process: the one that holds it will run it."""
        sent = frame("create", create_args(str(proj)))
        holder = target(switched_on, bridge_pid=os.getppid(), bridge_pid_start=None)
        receipt = holder.receive(sent)
        assert receipt.action == "run" and receipt.request_id is not None
        assert target(switched_on).run(receipt.request_id) is None
        assert switched_on.sleepers.read("nap") is None
        result = holder.run(receipt.request_id)
        assert result is not None and result["outcome"] == "ok"

    def test_one_queued_in_a_dead_process_runs_here_if_not_expired(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        sent = frame("create", create_args(str(proj)))
        target(switched_on, bridge_pid=dead_pid(), bridge_pid_start=None).receive(sent)
        result = deliver(target(switched_on), sent)
        assert result["outcome"] == "ok"
        assert switched_on.sleepers.read("nap") is not None

    def test_one_queued_in_a_dead_process_and_expired_is_not_run(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        sent = frame("create", create_args(str(proj)), expires_in=30)
        target(switched_on, bridge_pid=dead_pid(), bridge_pid_start=None).receive(sent)
        later = datetime.now(UTC) + timedelta(seconds=60)
        result = deliver(target(switched_on, clock=lambda: later), sent)
        assert result["outcome"] == "expired"
        assert switched_on.sleepers.read("nap") is None

    def test_the_record_is_keyed_by_peer(self, switched_on: StateStore, proj: Path) -> None:
        """A request id is the requester's, only unique on its side."""
        sent = frame("list")
        assert deliver(target(switched_on), sent)["outcome"] == "ok"
        other = Target(switched_on, self_host="adolin", peer="mac", role="connect")
        assert other.receive(sent).action == "run"

    def test_old_records_are_pruned_and_recent_ones_kept(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        old = frame("list")
        deliver(target(switched_on), old)
        recent = frame("list")
        deliver(target(switched_on), recent)
        later = datetime.now(UTC) + RECORD_RETENTION - timedelta(hours=1)
        assert prune_records(switched_on, later) == 0
        assert prune_records(switched_on, later + timedelta(hours=2)) == 2
        assert list((switched_on.root / "sleeper-ops" / "in").glob("*/*.json")) == []


# ------------------------------------------------------------------ the other ops


class TestOperations:
    def test_list_returns_the_records_and_the_text(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        deliver(target(switched_on), frame("create", create_args(str(proj))))
        result = deliver(target(switched_on), frame("list"))
        assert result["outcome"] == "ok"
        assert [r["name"] for r in result["records"]] == ["nap"]
        assert result["stdout"].startswith("nap  ASLEEP  (agent-nap)")

    def test_sleep_wake_and_retire(self, switched_on: StateStore, proj: Path) -> None:
        t = target(switched_on)
        deliver(t, frame("create", create_args(str(proj))))
        assert deliver(t, frame("sleep", {"name": "nap"}))["stdout"] == (
            "sleeper nap: already asleep"
        )
        woken = deliver(t, frame("wake", {"name": "nap"}))  # conftest refuses tmux
        assert (woken["outcome"], woken["exit"]) == ("failed", 1)
        retired = deliver(t, frame("retire", {"name": "nap"}))
        assert retired["outcome"] == "ok", retired
        assert switched_on.sleepers.read("nap") is None
        gone = deliver(t, frame("retire", {"name": "nap"}))
        assert gone["stderr"] == "spanreed sleeper retire: there is no sleeper named 'nap'"


# ------------------------------------------------------------------ the audit log


class TestAuditLog:
    def test_a_request_logs_received_then_done_with_its_args_in_full(
        self, switched_on: StateStore, proj: Path
    ) -> None:
        args = create_args(str(proj), claude_args=["--mcp-config", "/home/u/servers.json"])
        sent = frame("create", args)
        deliver(target(switched_on), sent)
        received, done = audit(switched_on)
        assert received["event"] == "received"
        assert received["args"] == args
        assert received["requested_by"] == "jmonk on kaladin"
        assert received["expires_at"] == sent["expires_at"]
        for line in (received, done):
            assert line["request_id"] == sent["request_id"]
            assert (line["peer"], line["role"], line["op"]) == ("kaladin", "serve", "create")
            assert line["bridge_pid"] == os.getpid()
            assert line["ts"].endswith("Z")
        assert done["event"] == "done"
        assert (done["outcome"], done["exit"]) == ("ok", 0)
        assert done["summary"] == "created sleeper nap (agent-nap), ASLEEP"

    def test_a_failure_summary_is_the_error(self, switched_on: StateStore) -> None:
        deliver(target(switched_on), frame("wake", {"name": "ghost"}))
        done = audit(switched_on)[-1]
        assert (done["outcome"], done["exit"]) == ("failed", 1)
        assert done["summary"] == "spanreed sleeper wake: there is no sleeper named 'ghost'"

    def test_the_log_and_records_are_owner_only(self, switched_on: StateStore) -> None:
        deliver(target(switched_on), frame("list"))
        log = switched_on.root / "logs" / "sleeper-ops.jsonl"
        assert stat.S_IMODE(log.stat().st_mode) == 0o600
        (record,) = (switched_on.root / "sleeper-ops" / "in" / "kaladin").glob("*.json")
        assert stat.S_IMODE(record.stat().st_mode) == 0o600
        assert stat.S_IMODE(record.parent.stat().st_mode) == 0o700
