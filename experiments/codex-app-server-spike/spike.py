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
import base64
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

TIMEOUT = 15.0
# Discovery probes get a short fuse: step 2 tries 8 combinations, and a server
# that simply never answers a shape it dislikes would otherwise cost 8 x 15s.
# Two minutes of silence is a script nobody runs twice.
PROBE_TIMEOUT = 4.0
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


GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def ws_handshake(sock: socket.socket, host: str = "localhost", path: str = "/") -> bytearray:
    """RFC 6455 upgrade. Returns whatever body bytes arrived with the response.

    Run 3's log said exactly what was missing:

        failed to upgrade control socket websocket connection:
        WebSocket protocol error: httparse error: invalid token

    The unix socket is a WebSocket control socket, so raw JSON was being parsed
    as an HTTP request line. None of A1-A5 covered that — the assumption that
    broke was one I had not written down, which is its own lesson about
    enumerating assumptions.
    """
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("server closed during the WebSocket handshake")
        buf.extend(chunk)
    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    status = head.split(b"\r\n")[0].decode(errors="replace")
    if "101" not in status:
        raise RuntimeError(
            f"no upgrade: {status!r}; headers: {head.decode(errors='replace')[:200]}"
        )
    want = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    if want.lower() not in head.decode(errors="replace").lower():
        raise RuntimeError("Sec-WebSocket-Accept did not match — not a conformant upgrade")
    return bytearray(rest)


def ws_encode(payload: bytes, opcode: int = 0x1) -> bytes:
    """A client frame. Masking is mandatory client->server; an unmasked frame is
    a protocol error the server must close on, which would look exactly like the
    failure this is fixing."""
    mask = os.urandom(4)
    n = len(payload)
    if n < 126:
        hdr = struct.pack("!BB", 0x80 | opcode, 0x80 | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, n)
    return hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def ws_decode(buf: bytearray) -> tuple[int, bytes, int] | None:
    """One frame, or None if more bytes are needed. (opcode, payload, consumed)."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    opcode, masked, n = b0 & 0x0F, b1 & 0x80, b1 & 0x7F
    off = 2
    if n == 126:
        if len(buf) < 4:
            return None
        n = struct.unpack("!H", bytes(buf[2:4]))[0]
        off = 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n = struct.unpack("!Q", bytes(buf[2:10]))[0]
        off = 10
    mask = b""
    if masked:
        if len(buf) < off + 4:
            return None
        mask = bytes(buf[off : off + 4])
        off += 4
    if len(buf) < off + n:
        return None
    payload = bytes(buf[off : off + n])
    if masked:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload, off + n


def frame(payload: bytes, style: str) -> bytes:
    """`jsonl` = one JSON object per line. `lsp` = a Content-Length header first,
    as LSP and MCP-over-stdio use. Run 1 assumed jsonl and the server hung up
    without a word, which does not distinguish wrong framing from wrong params."""
    if style == "ws":
        return ws_encode(payload)
    if style == "lsp":
        return b"Content-Length: %d\r\n\r\n%s" % (len(payload), payload)
    return payload + b"\n"


def jsonrpc(
    sock: socket.socket,
    buf: bytearray,
    method: str,
    params: Any,
    mid: int,
    style: str = "jsonl",
    timeout: float = TIMEOUT,
) -> Any:
    """One request, one matching response. Raises on timeout or transport error.

    Reads newline-delimited JSON (A2). Notifications and responses to other ids
    are skipped rather than mistaken for ours — the server streams thread events
    on the same connection, so an unfiltered read would return the wrong object
    and every assertion after it would be about that object.
    """
    payload = json.dumps({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
    sock.sendall(frame(payload.encode(), style))

    deadline = time.monotonic() + timeout
    while True:
        if style == "ws":
            while (got := ws_decode(buf)) is not None:
                opcode, payload, used = got
                del buf[:used]
                if opcode == 0x8:
                    raise RuntimeError("server sent a WebSocket close frame")
                if opcode == 0x9:  # ping -> pong, or it hangs up on us
                    sock.sendall(ws_encode(payload, 0xA))
                    continue
                if opcode not in (0x1, 0x2):
                    continue
                msg = json.loads(payload.decode(errors="replace"))
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"server returned an error: {msg['error']}")
                    return msg.get("result")
                continue
        while b"\n" in buf:
            line, _, rest = bytes(buf).partition(b"\n")
            buf.clear()
            buf.extend(rest)
            if not line.strip():
                continue
            text = line.decode(errors="replace").strip()
            if not text or (":" in text.split("{")[0][:20] and text.lower().startswith("content-")):
                continue  # LSP header; the body follows a blank line
            try:
                msg = json.loads(text)
            except json.JSONDecodeError as exc:
                raise RuntimeError(f"not JSON on this framing (A2): {text[:110]!r}") from exc
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"server returned an error: {msg['error']}")
                return msg.get("result")
            # else: a notification or another id — keep reading.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no response to {method} within {timeout}s")
        sock.settimeout(remaining)
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError(f"server closed the connection during {method}")
        buf.extend(chunk)


def dump_schema(codex: str) -> list[tuple[str, Any]]:
    """Ask Codex for its own schema instead of guessing a fifth time.

    `codex app-server generate-json-schema --out DIR` writes the authoritative
    request shapes. Run 2 tried eight hand-written combinations and the server
    closed the connection on all eight, including empty params — which is not
    what parameter validation looks like, so the shape was probably never the
    problem. Reading beats guessing either way.
    """
    out = Path(tempfile.mkdtemp(prefix="spanreed-schema-"))
    r = subprocess.run(
        [codex, "app-server", "generate-json-schema", "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        print(
            f"  generate-json-schema exited {r.returncode}: {(r.stderr or r.stdout).strip()[:300]}"
        )
        return []
    files = sorted(out.rglob("*"))
    print(f"  wrote {len([f for f in files if f.is_file()])} file(s) to {out}")
    found: list[tuple[str, Any]] = []
    for f in files:
        if not f.is_file() or f.suffix not in (".json", ".ts", ".txt", ""):
            continue
        try:
            text = f.read_text(errors="replace")
        except OSError:
            continue
        if "initialize" not in text.lower():
            continue
        print(f"  {f.name}: mentions initialize ({len(text)} bytes)")
        try:
            found.append((f.name, json.loads(text)))
        except json.JSONDecodeError:
            for line in text.splitlines():
                if "initialize" in line.lower():
                    print(f"      {line.strip()[:150]}")
    return found


def try_stdio(codex: str, params: Any) -> str:
    """Control: the SAME message over the stdio transport.

    stdio is the documented default; unix and websocket are flagged
    experimental. If stdio accepts what unix rejects, the transport is the
    problem and the params are fine — which is a different answer from both
    "Codex declines" and "the script guessed wrong", and neither of the first
    two runs could have told them apart.
    """
    proc = subprocess.Popen(
        [codex, "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    try:
        out, err = proc.communicate(req + "\n", timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
        return "no reply within 20s (process still running — may be waiting for more input)"
    reply = (out or "").strip().splitlines()
    if reply:
        return f"REPLIED: {reply[0][:220]}"
    return f"no stdout. exit={proc.returncode}. stderr: {(err or '').strip()[:220] or '(silent)'}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--turn",
        metavar="THREAD_ID",
        help="also run step 4: start ONE short turn in this thread. "
        "Get the id from step 3's output, or from a session you have open.",
    )
    ap.add_argument(
        "--connect",
        metavar="SOCKET",
        help="connect to an EXISTING app-server socket instead of spawning one. "
        "This is the only way to reach threads another process owns — a spawned "
        "server has its own state and sees nothing of the TUI's.",
    )
    ap.add_argument("--keep-socket", action="store_true", help="don't delete the socket dir")
    ap.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="seconds to wait per operation (default 60). Run 5 saw thread/list and "
        "thread/start exceed 15s — thread/start may be booting a real session, and "
        "the earlier log showed online model fetches, so slow is plausible.",
    )
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

    # ---- step 0: what already exists ----------------------------------------
    # Run 5 settled something the first five runs could not: a human talked to
    # `codex` in another terminal and step 3 still saw nothing. That is not a
    # bug — every run has been spawning its OWN app-server, a separate process
    # with separate state. A private server cannot see another process's
    # threads, so the question was never being asked.
    #
    # If the TUI (or the App, or the VS Code extension) already has a socket,
    # connecting to THAT is the whole game.
    hr("Step 0 — what is already on this machine?")
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    print(f"  CODEX_HOME: {home}  exists={home.exists()}")
    existing: list[Path] = []
    if home.exists():
        for sub in sorted(home.iterdir()):
            kind = "dir" if sub.is_dir() else "file"
            extra = ""
            if sub.is_dir():
                try:
                    extra = f" ({sum(1 for _ in sub.rglob('*') if _.is_file())} files)"
                except OSError:
                    extra = " (unreadable)"
            print(f"    {kind:4} {sub.name}{extra}")
        for sock_file in home.rglob("*.sock"):
            existing.append(sock_file)
    if existing:
        print("\n  EXISTING SOCKETS — these belong to processes already running:")
        for e in existing:
            print(f"    {e}")
        print("  If one of these is a live app-server, it is the one that can see")
        print("  the human's threads. Re-run with --connect <path> to use it.")
    else:
        print("\n  No existing sockets under CODEX_HOME.")
        print("  Note what that means: nothing here is serving the TUI's threads, so")
        print("  a spawned server necessarily starts empty. That is consistent with")
        print("  step 3 staying empty after you talked to codex in another terminal.")
    procs = subprocess.run(["pgrep", "-af", "codex"], capture_output=True, text=True)
    lines = [ln for ln in (procs.stdout or "").splitlines() if "spike" not in ln]
    print(f"\n  codex processes running: {len(lines)}")
    for ln in lines[:8]:
        print(f"    {ln[:150]}")

    # ---- step 1: start the server -------------------------------------------
    hr("Step 1 — start app-server on a unix socket")
    if args.connect:
        print(f"  SKIPPED: --connect {args.connect} given; using an existing socket instead.")
        sock_path = Path(args.connect)
        proc = None
        tmp = None
        if not sock_path.exists():
            s1.no(f"{sock_path} does not exist")
            return report(steps, args)
        s1.ok(f"using existing socket {sock_path} (not spawned by this script)")
        print(f"  {s1.verdict}: {s1.detail}")
    else:
        tmp = Path(tempfile.mkdtemp(prefix="spanreed-spike-"))
        sock_path = tmp / "app.sock"
        cmd = [codex, "app-server", "--listen", f"unix://{sock_path}"]
        print(f"  $ {' '.join(cmd)}")
        # RUST_LOG: app-server is Rust and said nothing at all on run 2. If it can
        # be made to explain why it hangs up, this is the switch that does it.
        env = {**os.environ, "RUST_LOG": os.environ.get("RUST_LOG", "info")}
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env
        )

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
    # A matrix, not a guess. Run 1 sent one shape on one framing and the server
    # closed the connection with no error body — which cannot distinguish "wrong
    # framing" from "wrong params". Every combination is now tried, each on a
    # FRESH connection, because a rejected message closes the one it arrived on.
    hr("Step 2a — what does Codex say its own initialize looks like?")
    schema = dump_schema(codex)
    if not schema:
        print("  (no schema recovered — the matrix below is still a guess)")

    hr("Step 2b — connect as a second client and initialize")
    client = {"name": "spanreed-spike", "version": "0.0.0"}
    shapes: list[tuple[str, Any]] = [
        ("mcp-style", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": client}),
        ("clientInfo-only", {"clientInfo": client}),
        ("flat", client),
        ("empty", {}),
    ]
    sock: socket.socket | None = None
    buf = bytearray()
    wire = "jsonl"
    for style in ("ws", "jsonl", "lsp"):
        for label, params in shapes:
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(TIMEOUT)
                probe.connect(str(sock_path))
            except OSError as exc:
                s2.no(f"could not connect to the socket at all: {exc}")
                return report(steps, args, proc, tmp, args.keep_socket)
            pbuf = bytearray()
            try:
                probe.settimeout(PROBE_TIMEOUT)
                if style == "ws":
                    pbuf = ws_handshake(probe)
                result = jsonrpc(probe, pbuf, "initialize", params, 1, style, PROBE_TIMEOUT)
            except Exception as exc:
                print(f"  {style:5} / {label:16} no  -- {type(exc).__name__}: {str(exc)[:88]}")
                probe.close()
                continue
            print(f"  {style:5} / {label:16} YES -- {json.dumps(result)[:110]}")
            s2.ok(f"framing={style}, params={label}; returned {json.dumps(result)[:150]}")
            probe.settimeout(TIMEOUT)
            # Carry the winning framing forward. Steps 3-4 previously used the
            # default, so a WebSocket connection got raw JSON lines and the
            # server hung up — a bug found only by testing against a real
            # implementation rather than one that agreed with me.
            wire = style
            sock, buf = probe, pbuf
            break
        if sock is not None:
            break

    if sock is None:
        hr("Step 2c — CONTROL: the same message over stdio")
        print("  stdio is the documented default; unix/ws are experimental.")
        verdict = try_stdio(codex, {"clientInfo": client})
        print(f"  stdio initialize -> {verdict}")
        if verdict.startswith("REPLIED"):
            s2.no(
                "unix socket rejected every combination, but STDIO ANSWERED the same "
                "message. The params are fine and the unix transport is the problem — "
                "so the design is not dead, it just cannot use this transport."
            )
            return report(steps, args, proc, tmp, args.keep_socket)
        s2.no(
            "every framing x params combination was rejected (A2/A3). Run "
            "`codex app-server generate-json-schema` there — it emits the authoritative "
            "request shape, which beats another round of guessing."
        )
        return report(steps, args, proc, tmp, args.keep_socket)
    print(f"  {s2.verdict}: {s2.detail}")

    # ---- step 3: discover threads -------------------------------------------
    hr("Step 3 — list threads")
    found: list[str] = []
    created = False
    for mid, method, params in ((2, "thread/loaded/list", {}), (3, "thread/list", {"limit": 10})):
        t0 = time.monotonic()
        try:
            res = jsonrpc(sock, buf, method, params, mid, wire, args.timeout)
        except Exception as exc:
            print(
                f"  {method:20} FAILED after {time.monotonic() - t0:.1f}s  {type(exc).__name__}: {exc}"
            )
            continue
        print(f"  {method:20} ({time.monotonic() - t0:.1f}s)", end=" ")
        ids = extract_ids(res)
        # Deduplicate across methods: a live thread appears in BOTH listings, and
        # summing them reported 3 threads for 2. A count that overstates is the
        # kind of number someone quotes back later.
        found.extend(i for i in ids if i not in found)
        # The denominator matters: "0 threads" and "the call did not work" must
        # not look the same, which is why the raw shape is printed on zero.
        if ids:
            print(f"{len(ids)} thread(s): {', '.join(ids[:5])}")
        else:
            print(f"0 threads. Raw: {json.dumps(res)[:160]}")

    if not found:
        print("\n  No threads exist. Creating one so the turn path is still testable —")
        print("  this answers 'can this client drive a turn AT ALL', which is most of")
        print("  the protocol risk. It does NOT answer 'someone else's open thread'.")
        for params in ({"cwd": str(Path.home())}, {}):
            t0 = time.monotonic()
            try:
                res = jsonrpc(sock, buf, "thread/start", params, 10, wire, args.timeout)
            except Exception as exc:
                print(
                    f"  thread/start {json.dumps(params)[:28]:30} no after "
                    f"{time.monotonic() - t0:.1f}s -- {str(exc)[:70]}"
                )
                continue
            ids = extract_ids(res)
            print(f"  thread/start {json.dumps(params)[:30]:32} YES -- {json.dumps(res)[:110]}")
            if ids:
                found.extend(ids)
                created = True
                break

    if found and created:
        s3.verdict = "PARTIAL"
        s3.detail = (
            f"{len(found)} thread id(s), but all created by this script — "
            f"`thread/list` returned empty, so nothing shows whether a human's open "
            f"session is visible here. That is the half that matters."
        )
    elif found:
        s3.ok(f"{len(found)} distinct thread id(s) visible to a non-owning client")
    else:
        s3.no(
            "connected and called both methods, but no thread ids came back — "
            "open a Codex session and re-run, or the shape differs from A4"
        )
    print(f"  {s3.verdict}: {s3.detail}")

    # ---- step 4: start a turn ------------------------------------------------
    hr("Step 4 — start a turn in a thread someone else has open")
    target = args.turn or (found[0] if created and found else None)
    if target and not args.turn:
        print(f"  no --turn given, but a thread was just created: using {target}")
        print("  NOTE: this is OUR thread, not one a human has open. A pass here means")
        print("  the protocol works; it does not yet mean we can reach someone else's.")
    if not target:
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
        print(f"  thread : {target}")
        print(f"  prompt : {prompt[:80]}...")
        try:
            jsonrpc(sock, buf, "thread/resume", {"threadId": target}, 4, wire)
        except Exception as exc:
            print(
                f"  thread/resume FAILED  {type(exc).__name__}: {exc}  (may be fine — trying turn/start anyway)"
            )
        try:
            res = jsonrpc(
                sock,
                buf,
                "turn/start",
                {"threadId": target, "input": [{"type": "text", "text": prompt}]},
                5,
                wire,
            )
            if created:
                # Do NOT call this a pass. The question is "a thread someone
                # else is using" and this thread is ours. Reporting PASS here
                # would be a verdict claiming more than the run showed — the
                # defect this script exists to avoid, in the script's own
                # output.
                s4.verdict = "PARTIAL"
                s4.detail = (
                    f"turn/start accepted ({json.dumps(res)[:120]}) — but on a thread THIS "
                    f"SCRIPT created. The protocol works end to end. Whether a NON-OWNING "
                    f"client may drive a thread a human has open is still unanswered: open "
                    f"`codex` in another terminal, re-run, and pass --turn with an id from "
                    f"step 3."
                )
            else:
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


def drain(proc: subprocess.Popen[str] | None) -> str:
    """Whatever app-server wrote to stdout/stderr. Run 1 printed this ONLY when
    the process died early, so a server that closed a connection and explained
    why had its explanation thrown away — the single most useful line in the
    run, discarded by the script meant to diagnose it."""
    if not proc or not proc.stdout:
        return ""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        return proc.stdout.read() or ""
    except Exception:
        return ""


def report(
    steps: list[Step],
    args: argparse.Namespace,
    proc: subprocess.Popen[str] | None = None,
    tmp: Path | None = None,
    keep: bool = False,
) -> int:
    server_output = drain(proc)
    if server_output.strip():
        hr("What app-server itself said")
        # Run 3's single useful line — the websocket upgrade failure — arrived
        # buried in forty enter/exit traces of the same span. WARN/ERROR first,
        # INFO deduped by message, because a diagnostic nobody reads is not one.
        warns, infos, seen = [], [], set()
        for line in server_output.strip().splitlines():
            if " WARN " in line or " ERROR " in line:
                warns.append(line)
                continue
            # Key on the span NAME plus the trailing message, not on a split
            # that lands inside the span's own fields — the previous key made
            # 120 lines of one repeated span look like 12 distinct ones.
            name = ""
            if 'otel.name="' in line:
                name = line.split('otel.name="', 1)[1].split('"', 1)[0]
            tail = line.rsplit("}: ", 1)[-1].rsplit(": ", 1)[-1].strip()
            key = f"{name}|{tail}"
            if key not in seen:
                seen.add(key)
                infos.append(f"{name or '-':24} {tail}")
        for line in warns:
            print(f"  {line[:240]}")
        if warns and infos:
            print(f"  -- {len(infos)} distinct INFO line(s) follow --")
        for line in infos[-12:]:
            print(f"  {line[:200]}")
        total = len(server_output.strip().splitlines())
        print(f"  [{len(warns)} warn/error, {total} lines total; INFO deduped]")
    hr("Result")
    for s in steps:
        print(f"  {s.n}. [{s.verdict:11}] {s.question}\n        {s.detail}")

    verdicts = {s.n: s.verdict for s in steps}
    print()
    if verdicts[4] == "PARTIAL":
        print("  The protocol works: connect, list, create, and drive a turn — all of it")
        print("  over the unix WebSocket, from a process Codex did not spawn.")
        print("  NOT yet shown: that a thread a HUMAN has open is visible and drivable.")
        print("  That needs a live `codex` session and a re-run. It is the whole question.")
    elif verdicts[4] == "PASS":
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

    return 0 if verdicts[4] in ("PASS", "PARTIAL") else 1


if __name__ == "__main__":
    sys.exit(main())
