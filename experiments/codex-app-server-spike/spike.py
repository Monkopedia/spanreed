#!/usr/bin/env python3
"""Can a second process start a turn in a Codex session someone else has open?

That is the only question. If the answer is yes, spanreed can treat a Codex
session as a real peer on the bus — mail arrives, the monitor calls turn/start,
the agent wakes with the human watching. If it is no, a Codex agent can only be
a mailbox that gets read at hook boundaries, and the integration is worth much
less.

Run this on a machine with Codex signed in. It answers four questions in order,
each gating the next. Nothing is installed, no repo is touched, and the only
side effect is one short turn in a thread you nominate (step 4, opt-in).

    python3 spike.py                     # steps 1-3, read-only
    python3 spike.py --turn <thread-id>  # all four, starts ONE turn

WHY THIS SCRIPT IS SHAPED THE WAY IT IS
---------------------------------------
Written against documentation that could not be executed — the target machine
has Codex, this one does not. Everything below is therefore an ASSUMPTION until
it runs, and the script's main job is to tell you WHICH assumption broke rather
than just failing.

So: every step prints PASS / FAIL / SKIP with a reason, no step is silent, and
a step that cannot run says so instead of reporting nothing. An empty result and
a result of empty are never formatted the same way. That distinction is the
whole point — a spike that says "no threads found" when it never connected is
worse than one that crashes.

Assumptions, each one a place this can be wrong without Codex being wrong:
  A1  `codex app-server --listen unix://PATH` is the right invocation
  A2  the unix transport frames messages as newline-delimited JSON, like stdio
  A3  `initialize` is required first, per connection
  A4  method names are thread/list, thread/loaded/list, turn/start
  A5  turn/start takes a threadId and an input message

If a step fails, the printed reason names the assumption, so you can tell
"Codex will not do this" from "this script guessed the wire format wrong".
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

TIMEOUT = 15.0
SPIKE_MARKER = "SPANREED_SPIKE_OK"


class Step:
    """One question, its verdict, and why — printed even when it cannot run."""

    def __init__(self, n: int, question: str) -> None:
        self.n = n
        self.question = question
        self.verdict = "DID NOT RUN"
        self.detail = "the step before it did not get far enough"

    def ok(self, detail: str) -> None:
        self.verdict, self.detail = "PASS", detail

    def no(self, detail: str) -> None:
        self.verdict, self.detail = "FAIL", detail

    def skip(self, detail: str) -> None:
        self.verdict, self.detail = "SKIP", detail


def hr(title: str) -> None:
    print(f"\n{'=' * 72}\n{title}\n{'=' * 72}")


def jsonrpc(sock: socket.socket, buf: bytearray, method: str, params: Any, mid: int) -> Any:
    """One request, one matching response. Raises on timeout or transport error.

    Reads newline-delimited JSON (A2). Notifications and responses to other ids
    are skipped rather than mistaken for ours — the server streams thread events
    on the same connection, so an unfiltered read would return the wrong object
    and every assertion after it would be about that object.
    """
    payload = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
    sock.sendall(payload.encode() + b"\n")

    deadline = time.monotonic() + TIMEOUT
    while True:
        while b"\n" in buf:
            line, _, rest = bytes(buf).partition(b"\n")
            buf.clear()
            buf.extend(rest)
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"not newline-delimited JSON (A2): {line[:120]!r}") from exc
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"server returned an error: {msg['error']}")
                return msg.get("result")
            # else: a notification or another id — keep reading.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no response to {method} within {TIMEOUT}s")
        sock.settimeout(remaining)
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError(f"server closed the connection during {method}")
        buf.extend(chunk)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--turn",
        metavar="THREAD_ID",
        help="also run step 4: start ONE short turn in this thread. "
        "Get the id from step 3's output, or from a session you have open.",
    )
    ap.add_argument("--keep-socket", action="store_true", help="don't delete the socket dir")
    args = ap.parse_args()

    steps = [
        Step(1, "Does `codex app-server` start under this machine's sign-in?"),
        Step(2, "Can a separate process connect and `initialize`?"),
        Step(3, "Can it see threads — including ones a human has open?"),
        Step(4, "Can it start a turn in a thread someone else is using?"),
    ]
    s1, s2, s3, s4 = steps

    hr("Environment")
    codex = shutil.which("codex")
    print(f"  codex on PATH : {codex or 'NOT FOUND'}")
    if not codex:
        s1.no("`codex` is not on PATH — nothing else can be attempted")
        return report(steps, args)
    ver = subprocess.run([codex, "--version"], capture_output=True, text=True, timeout=20)
    print(f"  version       : {ver.stdout.strip() or ver.stderr.strip() or '(no output)'}")
    print(f"  CODEX_HOME    : {os.environ.get('CODEX_HOME', '(unset, defaults to ~/.codex)')}")

    # ---- step 1: start the server -------------------------------------------
    hr("Step 1 — start app-server on a unix socket")
    tmp = Path(tempfile.mkdtemp(prefix="spanreed-spike-"))
    sock_path = tmp / "app.sock"
    cmd = [codex, "app-server", "--listen", f"unix://{sock_path}"]
    print(f"  $ {' '.join(cmd)}")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    for _ in range(60):  # up to ~15s
        if sock_path.exists():
            break
        if proc.poll() is not None:
            out = (proc.stdout.read() if proc.stdout else "") or "(no output)"
            s1.no(
                f"exited {proc.returncode} before creating the socket (A1). Output:\n      "
                + out.strip().replace("\n", "\n      ")
            )
            return report(steps, args, proc, tmp, args.keep_socket)
        time.sleep(0.25)
    else:
        s1.no(
            f"ran for 15s without creating {sock_path} (A1) — wrong flag, or a different transport"
        )
        return report(steps, args, proc, tmp, args.keep_socket)

    s1.ok(f"socket appeared at {sock_path}")
    print(f"  {s1.verdict}: {s1.detail}")

    # ---- step 2: connect and initialize --------------------------------------
    hr("Step 2 — connect as a second client and initialize")
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(TIMEOUT)
        sock.connect(str(sock_path))
    except OSError as exc:
        s2.no(f"could not connect to the socket: {exc}")
        return report(steps, args, proc, tmp, args.keep_socket)

    buf = bytearray()
    try:
        result = jsonrpc(
            sock,
            buf,
            "initialize",
            {"clientInfo": {"name": "spanreed-spike", "version": "0"}},
            1,
        )
        s2.ok(f"initialize returned: {json.dumps(result)[:200]}")
    except Exception as exc:
        s2.no(f"{type(exc).__name__}: {exc}  (A2/A3)")
        return report(steps, args, proc, tmp, args.keep_socket)
    print(f"  {s2.verdict}: {s2.detail}")

    # ---- step 3: discover threads -------------------------------------------
    hr("Step 3 — list threads")
    found: list[str] = []
    for mid, method, params in ((2, "thread/loaded/list", {}), (3, "thread/list", {"limit": 10})):
        try:
            res = jsonrpc(sock, buf, method, params, mid)
        except Exception as exc:
            print(f"  {method:20} FAILED  {type(exc).__name__}: {exc}")
            continue
        ids = extract_ids(res)
        # Deduplicate across methods: a live thread appears in BOTH listings, and
        # summing them reported 3 threads for 2. A count that overstates is the
        # kind of number someone quotes back later.
        found.extend(i for i in ids if i not in found)
        # The denominator matters: "0 threads" and "the call did not work" must
        # not look the same, which is why the raw shape is printed on zero.
        if ids:
            print(f"  {method:20} {len(ids)} thread(s): {', '.join(ids[:5])}")
        else:
            print(f"  {method:20} 0 threads. Raw result: {json.dumps(res)[:200]}")

    if found:
        s3.ok(f"{len(found)} distinct thread id(s) visible to a non-owning client")
    else:
        s3.no(
            "connected and called both methods, but no thread ids came back — "
            "open a Codex session and re-run, or the shape differs from A4"
        )
    print(f"  {s3.verdict}: {s3.detail}")

    # ---- step 4: start a turn ------------------------------------------------
    hr("Step 4 — start a turn in a thread someone else has open")
    if not args.turn:
        s4.skip(
            "not requested. Re-run with --turn <thread-id> to test this. "
            "THIS IS THE QUESTION THAT MATTERS — steps 1-3 passing without it "
            "only shows you can look, not that you can act."
        )
    else:
        prompt = (
            f"This is an automated connectivity probe. Reply with exactly "
            f"{SPIKE_MARKER} and nothing else. Do not use any tools, do not read "
            f"or modify any files, do not run any commands."
        )
        print(f"  thread : {args.turn}")
        print(f"  prompt : {prompt[:80]}...")
        try:
            jsonrpc(sock, buf, "thread/resume", {"threadId": args.turn}, 4)
        except Exception as exc:
            print(
                f"  thread/resume FAILED  {type(exc).__name__}: {exc}  (may be fine — trying turn/start anyway)"
            )
        try:
            res = jsonrpc(
                sock,
                buf,
                "turn/start",
                {"threadId": args.turn, "input": [{"type": "text", "text": prompt}]},
                5,
            )
            s4.ok(f"turn/start accepted: {json.dumps(res)[:200]}")
        except Exception as exc:
            s4.no(
                f"{type(exc).__name__}: {exc}  (A5 — or turns from a non-owning client are refused)"
            )
    print(f"  {s4.verdict}: {s4.detail}")

    return report(steps, args, proc, tmp, args.keep_socket)


def extract_ids(result: Any) -> list[str]:
    """Pull thread ids out without assuming the response shape (A4).

    The schema is knowable — `codex app-server generate-json-schema` emits it —
    but this script is written without having run that, so it looks for ids
    structurally instead of indexing a shape it has not seen.
    """
    out: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, val in node.items():
                if key in ("threadId", "thread_id", "id") and isinstance(val, str):
                    out.append(val)
                else:
                    walk(val)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(result)
    return out


def report(
    steps: list[Step],
    args: argparse.Namespace,
    proc: subprocess.Popen[str] | None = None,
    tmp: Path | None = None,
    keep: bool = False,
) -> int:
    hr("Result")
    for s in steps:
        print(f"  {s.n}. [{s.verdict:11}] {s.question}\n        {s.detail}")

    verdicts = {s.n: s.verdict for s in steps}
    print()
    if verdicts[4] == "PASS":
        print("  A non-owning process can start a turn in an open thread.")
        print(f"  CHECK THE HUMAN'S CODEX WINDOW: did it show a turn replying {SPIKE_MARKER}?")
        print("  If it did, a Codex session can be a real peer on the bus, not a mailbox.")
        print("  If the turn ran but the human saw nothing, that is the ANSWER TO A DIFFERENT")
        print("  QUESTION and worth reporting separately — it would mean turns are possible")
        print("  but invisible, which is not good enough.")
    elif verdicts[4] == "SKIP":
        print("  Steps 1-3 say you can connect and look. They do NOT answer the question.")
        print("  Re-run with --turn <thread-id> using an id from step 3.")
    elif verdicts[1] != "PASS":
        print("  Blocked at the first step. Nothing about the design is settled either way —")
        print("  check whether `app-server` exists in this Codex version before concluding.")
    else:
        print("  The blocking failure is above, with the assumption it implicates.")
        print("  A1/A2/A3/A4/A5 failing means THIS SCRIPT guessed wrong.")
        print("  A clean protocol error from the server means CODEX declines to do it.")
        print("  Those are different answers; do not report one as the other.")

    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    if tmp and tmp.exists() and not keep:
        for p in tmp.iterdir():
            p.unlink(missing_ok=True)
        tmp.rmdir()
    elif tmp and keep:
        print(f"\n  socket dir kept at {tmp}")

    return 0 if verdicts[4] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
