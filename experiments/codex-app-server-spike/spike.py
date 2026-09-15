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
import atexit
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import ssl
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


MISSING = object()


def find_key(obj, key):
    """First value for `key` anywhere in a nested JSON structure, else MISSING.

    Returned as-is, so callers can tell False apart from absent -- the whole
    point here is that an explicit false is a different answer from silence.
    """
    stack = [obj]
    while stack:
        cur = stack.pop(0)
        if isinstance(cur, dict):
            if key in cur:
                return cur[key]
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    return MISSING


def frame(payload: bytes, style: str) -> bytes:
    """`jsonl` = one JSON object per line. `lsp` = a Content-Length header first,
    as LSP and MCP-over-stdio use. Run 1 assumed jsonl and the server hung up
    without a word, which does not distinguish wrong framing from wrong params."""
    if style == "ws":
        return ws_encode(payload)
    if style == "lsp":
        return b"Content-Length: %d\r\n\r\n%s" % (len(payload), payload)
    return payload + b"\n"


def notify(sock: socket.socket, method: str, params: Any, style: str) -> None:
    """A JSON-RPC notification: no id, no reply expected.

    `initialized` is required. From the app-server docs: clients must send one
    `initialize` request per connection, "then acknowledge with an initialized
    notification. The server rejects any request on that connection before this
    handshake."

    Nine runs missed this. `initialize` answered, so the connection looked
    established, and every call after it hung — which reads as a broken method
    rather than an incomplete handshake. The answer was in the docs the whole
    time and in ClientNotification.json, 431 bytes, sitting in CODEX_HOME and
    listed by name in this script's own output for five runs.
    """
    payload = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
    sock.sendall(frame(payload.encode(), style))


def jsonrpc(
    sock: socket.socket,
    buf: bytearray,
    method: str,
    params: Any,
    mid: int,
    style: str = "jsonl",
    timeout: float = TIMEOUT,
    on_notify: Any = None,
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
                nm = msg.get("method")
                if nm and msg.get("id") is not None:
                    # A request, not a notification. Unanswered, it hangs the
                    # server -- this is the bug that made every backend-touching
                    # call time out with the span still open.
                    handle_server_request(sock, msg, style)
                    continue
                if nm and on_notify is not None:
                    on_notify(nm, msg.get("params") or {})
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
            nm = msg.get("method")
            if nm and msg.get("id") is not None:
                handle_server_request(sock, msg, style)
                continue
            if nm and on_notify is not None:
                on_notify(nm, msg.get("params") or {})
            # else: a response to another id — keep reading.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no response to {method} within {timeout}s")
        sock.settimeout(remaining)
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError(f"server closed the connection during {method}")
        buf.extend(chunk)


SCHEMA_PARAMS: dict[str, dict[str, Any]] = {}

# Facts worth seeing even when the middle of a 300-line run is lost to
# truncation. Run 16 was pasted back with step 0 cut off — which is where the
# reachability answer lives, the single thing that run existed to produce. A
# diagnostic placed where it gets trimmed is one that was not produced.
FACTS: list[str] = []


# Every server->client REQUEST this run saw. This is the headline finding when
# a call hangs: app-server asks the client questions mid-call, and an unanswered
# one blocks the span forever with no error and no timeout -- which is exactly
# what runs 14-20 looked like. Reported independently by at least four other
# clients; see the README for the issue links.
# The socket dir prefix, used both to CREATE our temp dir and to RECOGNISE a
# server an earlier run left behind. One constant, because a detector that
# drifts from the thing it detects silently stops detecting.
SOCK_PREFIX = "spanreed-spike-"

SEEN_SERVER_REQUESTS: list[str] = []


def server_reply(
    sock: socket.socket, rid: Any, style: str, result: Any = None, error: Any = None
) -> None:
    """Answer a server-initiated request.

    The `"jsonrpc": "2.0"` member is omitted deliberately: app-server leaves it
    off its own frames, and clients that require it drop the peer's messages.
    """
    msg: dict[str, Any] = {"id": rid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = result
    sock.sendall(frame(json.dumps(msg).encode(), style))


def handle_server_request(sock: socket.socket, msg: dict[str, Any], style: str) -> None:
    """Reply to a server request, and say out loud that it happened.

    Declining is the right default for a probe -- the prompt tells the model to
    touch nothing, so any approval ask is already off-script. A decline ends the
    turn with an answer; silence ends it with a hang, and an answer is the thing
    this spike exists to produce.
    """
    rid = msg.get("id")
    method = msg.get("method") or ""
    SEEN_SERVER_REQUESTS.append(method)
    print(f"  <= SERVER REQUEST  {method}  {json.dumps(msg.get('params') or {})[:110]}")
    low = method.lower()
    if "approval" in low:
        server_reply(sock, rid, style, result={"decision": "decline"})
        print("     -> answered decision=decline")
    elif "elicitation" in low:
        server_reply(sock, rid, style, result={"action": "decline"})
        print("     -> answered action=decline")
    else:
        server_reply(
            sock,
            rid,
            style,
            error={"code": -32601, "message": f"spanreed-spike does not implement {method}"},
        )
        print("     -> answered -32601 method not found (a refusal beats a stall)")


# Every app-server this process spawns. Cleanup used to live only at the end of
# report(), so a Ctrl-C or an exception left the server running -- and each
# survivor keeps contending for the same CODEX_HOME sqlite as the next run. That
# is a leak whose symptom is the NEXT run looking broken, which is the hardest
# kind to attribute.
SPAWNED: list[subprocess.Popen] = []


def _reap(*_: Any) -> None:
    for proc in SPAWNED:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def install_reaper() -> None:
    """Kill spawned servers on normal exit AND on the signals that skip it."""
    atexit.register(_reap)
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        prev = signal.getsignal(sig)

        def handler(signum: int, frame: Any, _prev: Any = prev) -> None:
            _reap()
            if callable(_prev):
                _prev(signum, frame)
            else:
                sys.exit(128 + signum)

        with contextlib.suppress(ValueError, OSError):
            signal.signal(sig, handler)


def is_stray_spike_server(ps_line: str) -> bool:
    """True only for an app-server THIS script leaked in an earlier run.

    A substring test on the whole ps line is not identity: the first version of
    this matched any process whose command line merely mentioned the socket
    prefix, which included the shell running the test that found the bug. With
    --kill-strays that is a SIGTERM to the user's shell.

    So require all three: the executable is actually `codex`, it is running
    `app-server`, and its socket is under our own prefix. A shell that merely
    quotes any of those fails the first test.
    """
    parts = ps_line.split(None, 1)
    if len(parts) != 2 or not parts[0].isdigit():
        return False
    pid, cmd = int(parts[0]), parts[1]
    if pid == os.getpid():
        return False
    argv0 = cmd.split()[0] if cmd.split() else ""
    if Path(argv0).name != "codex":
        return False
    return "app-server" in cmd and SOCK_PREFIX in cmd


def probe_auth(home: Path) -> None:
    """Say whether codex is signed in, without ever printing a credential.

    Only shapes and timestamps: which auth files exist, how big, how old, and
    whether a token looks expired. app-server answering `initialize` proves the
    process started, not that it has a usable account behind it -- and every
    call that hangs is one that needs the account.
    """
    candidates = [home / "auth.json", home / "auth" / "auth.json"]
    found = [c for c in candidates if c.is_file()]
    if not found:
        print(f"  no auth.json under {home} — this codex may not be signed in at all")
        FACTS.append("no auth.json found — codex may not be signed in")
        return
    for f in found:
        st = f.stat()
        age_h = (time.time() - st.st_mtime) / 3600
        print(f"  {f.name}: {st.st_size}B, last written {age_h:.1f}h ago")
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"    unreadable as JSON ({exc}) — cannot say whether it is valid")
            continue
        # Key NAMES only. Never a value: these files hold live credentials.
        keys = ", ".join(sorted(data.keys()))[:160]
        print(f"    keys: {keys}")
        for key in ("expires_at", "expiresAt", "expiry", "exp"):
            exp = find_key(data, key)
            if exp is not MISSING and isinstance(exp, (int, float)):
                left = (exp - time.time()) / 60
                state = "EXPIRED" if left < 0 else f"{left:.0f} min left"
                print(f"    {key}: {state}")
                FACTS.append(f"auth token {state}")
                break
        else:
            # Run 24 hit this branch and contributed NOTHING to Key facts, so
            # the paste read as if sign-in had been checked and was fine. An
            # inconclusive probe has to say it is inconclusive.
            print("    no expiry field recognised — cannot say if this token is valid")
            FACTS.append(f"auth.json present ({age_h:.0f}h old, keys: {keys[:60]}), expiry UNKNOWN")


SECRETISH = ("key", "token", "secret", "password", "passwd", "credential", "authorization")


class Tee:
    """Write to the terminal and to a file at once.

    Every run so far has been pasted back as its last screenful, because the
    interesting part -- step 0, which inventories sockets, config and processes
    -- scrolls off the top. Diagnostics that only exist in scrollback are
    diagnostics nobody reads, which this script has now demonstrated about six
    times. So write the whole run to a file and say where it is.
    """

    def __init__(self, stream: Any, path: Path) -> None:
        self.stream = stream
        self.fh = path.open("w", encoding="utf-8")

    def write(self, data: str) -> int:
        self.stream.write(data)
        self.fh.write(data)
        return len(data)

    def flush(self) -> None:
        self.stream.flush()
        self.fh.flush()

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.fh.close()


def probe_config(home: Path) -> None:
    """Print config.toml, redacted, and name the settings documented to hang.

    The official docs say thread/list and thread/start do not block, "unless:
    upstream model service is unavailable, sandbox initialization fails,
    required MCP servers fail to initialize". All three of those are decided by
    this file, which twenty-four runs never opened.
    """
    cfg = home / "config.toml"
    if not cfg.is_file():
        print(f"  no config.toml under {home} — defaults everywhere")
        FACTS.append("no config.toml — MCP/sandbox config RULED OUT")
        return
    try:
        text = cfg.read_text(errors="replace")
    except OSError as exc:
        print(f"  config.toml unreadable: {exc}")
        FACTS.append(f"config.toml unreadable: {exc}")
        return

    print(f"  config.toml — {len(text)} bytes, redacted:")
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        key = line.split("=")[0].strip().lower()
        if "=" in line and any(w in key for w in SECRETISH):
            line = f"{line.split('=')[0]}= <redacted>"
        print(f"    {line[:150]}")

    mcp = [ln.strip() for ln in text.splitlines() if ln.strip().startswith("[mcp_servers")]
    required = [ln.strip() for ln in text.splitlines() if "required" in ln.lower()]
    print(f"\n  MCP servers configured: {len(mcp)}")
    for m in mcp:
        print(f"    {m}")
    if required:
        print("  lines mentioning `required` — a required MCP server that fails to")
        print("  initialize makes thread/start and thread/resume fail outright:")
        for r in required:
            print(f"    {r}")
    # Unconditional, both ways: "configured: 0" is a result, not an absence.
    FACTS.append(
        f"config.toml: {len(mcp)} MCP server(s), {len(required)} `required` line(s)"
        + ("  <-- documented cause of thread/start hanging" if mcp else "")
    )


def probe_state_db(home: Path) -> None:
    """Read the thread store directly, with no app-server in the way.

    `thread/list` with useStateDbOnly answered in 0.0s for several runs and then
    began timing out. That call is the one documented to stay local, so if the
    file itself is readable here while app-server cannot answer from it, the
    fault is in the server or in contention for the file -- not in the protocol,
    and not in this client. Opening read-only means this probe cannot be the
    thing that locks it.
    """
    dbs = sorted(home.glob("thread_history*.sqlite"))
    if not dbs:
        print("  no thread_history*.sqlite under CODEX_HOME — nothing to read directly")
        return
    for db in dbs:
        sidecars = [x.name for x in home.glob(db.name + "-*")]
        print(f"  {db.name}  {db.stat().st_size}B  sidecars={sidecars or 'none'}")
        t0 = time.monotonic()
        try:
            # immutable=1 promises we will not write and skips locking entirely,
            # so a writer holding the file cannot block this read.
            con = sqlite3.connect(f"file:{db}?immutable=1", uri=True, timeout=5)
            try:
                tables = [
                    r[0]
                    for r in con.execute(
                        "select name from sqlite_master where type='table'"
                    ).fetchall()
                ]
                print(
                    f"    opened in {time.monotonic() - t0:.2f}s; tables: {', '.join(tables[:8])}"
                )
                for t in tables:
                    if "thread" in t.lower():
                        n = con.execute(f"select count(*) from {t}").fetchone()[0]
                        print(f"    {t}: {n} row(s)")
                        FACTS.append(f"state DB readable directly: {t} has {n} row(s)")
            finally:
                con.close()
        except sqlite3.Error as exc:
            print(f"    sqlite refused it after {time.monotonic() - t0:.2f}s: {exc}")
            FACTS.append(f"state DB NOT readable directly: {exc}")


def _minimal_params(target: dict[str, Any], cwd: str) -> dict[str, Any]:
    """Build the smallest params object satisfying `required`.

    Reading the schema and then sending an invented shape anyway would repeat
    the mistake the schema was fetched to fix. Values are chosen by declared
    type; anything with no sensible default is left out and reported, so a gap
    shows up as a gap rather than as a silently wrong request.
    """
    out: dict[str, Any] = {}
    props = target.get("properties") or {}
    for field in target.get("required", []):
        spec = props.get(field, {})
        types = spec.get("type", "")
        types = types if isinstance(types, list) else [types]
        if "cwd" in field.lower() or "path" in field.lower():
            out[field] = cwd
        elif "integer" in types or "number" in types:
            out[field] = 10
        elif "boolean" in types:
            out[field] = False
        elif "array" in types:
            out[field] = []
        elif "object" in types:
            out[field] = {}
        elif "string" in types:
            out[field] = ""
        elif "null" in types:
            out[field] = None
    return out


def _find_method(node: Any, method: str, depth: int = 0) -> Any:
    """Find the sub-schema describing `method`, without assuming the layout.

    JSON-Schema nests differently per generator, so this searches for a const or
    enum carrying the method name and returns its enclosing object — the params
    shape lives beside it whatever the surrounding structure.
    """
    if depth > 12:
        return None
    if isinstance(node, dict):
        # Match the ENCLOSING schema, not the name node. A first version
        # returned {"enum": ["thread/list"]} — technically a hit, and useless:
        # it confirms the method exists while dropping the params shape, which
        # is the only reason to look. Verified by testing the extractor on a
        # generator-shaped document before trusting its output.
        props = node.get("properties")
        if isinstance(props, dict):
            meth = props.get("method")
            if isinstance(meth, dict):
                val = meth.get("const", meth.get("enum"))
                if val == method or (isinstance(val, list) and method in val):
                    return node
        for val in node.values():
            hit = _find_method(val, method, depth + 1)
            if hit is not None:
                return hit
    elif isinstance(node, list):
        for item in node:
            hit = _find_method(item, method, depth + 1)
            if hit is not None:
                return hit
    return None


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
        # ClientRequest.json carries the params shapes and is processed
        # unconditionally. It was previously reached only because it happens to
        # contain the word "initialize" — an incidental filter that silently
        # skipped it the moment that stopped being true, taking the params
        # extraction with it and leaving only the invented fallback.
        if f.name != "ClientRequest.json" and "initialize" not in text.lower():
            continue
        # Print small files in full rather than announcing their names. Five
        # runs listed ClientNotification.json at 431 bytes without opening it;
        # it names the handshake notification that was missing the whole time.
        # Filename is checked BEFORE size. With the size test first, a small
        # ClientRequest.json took the dump branch and never reached the
        # extractor, so the params shapes were printed and not parsed — the
        # branch order decided whether the step did its job.
        if f.name == "ClientRequest.json":
            pass  # falls through to the extractor below
        elif len(text) <= 2500:
            print(f"  --- {f.name} ({len(text)} bytes) ---")
            for line in text.splitlines()[:40]:
                print(f"      {line[:160]}")
        if f.name == "ClientRequest.json":
            # The authoritative shape of every request. Run 10 printed
            # "too large to dump; grep it if needed" — telling the operator to go
            # and find the thing this step exists to find. Extract instead.
            print(f"  --- {f.name}: extracting the methods that are failing ---")
            try:
                doc = json.loads(text)
            except json.JSONDecodeError:
                print("      (not parseable)")
                continue
            defs = doc.get("definitions", {})
            for want in ("thread/list", "thread/resume", "thread/start", "turn/start"):
                hit = _find_method(doc, want)
                if not hit:
                    print(f"      {want}: not found by name in this schema")
                    continue
                # Resolve the params $ref. Run 11 printed
                # params: {"$ref": "#/definitions/ThreadListParams"} and stopped
                # there — which names the shape without showing it, while this
                # client sends an invented one. Following the ref is the whole
                # point of reading the schema.
                params = (hit.get("properties") or {}).get("params") or {}
                ref = params.get("$ref", "")
                target = defs.get(ref.rsplit("/", 1)[-1]) if ref else params
                if target is None:
                    print(f"      {want}: params -> {ref} (not in definitions)")
                    continue
                req = target.get("required", [])
                props = list((target.get("properties") or {}).keys())
                print(f"      {want}")
                print(f"          required: {req}")
                print(f"          accepts : {props}")
                for field in req:
                    spec = (target.get("properties") or {}).get(field, {})
                    print(f"          {field}: {json.dumps(spec)[:140]}")
                    # Follow one more level. turn/start's `input` is an array of
                    # UserInput, and {"type":"text","text":...} has been assumed
                    # since the first draft without ever being checked — the same
                    # move that made thread/list's params invented for 12 runs.
                    nested = (spec.get("items") or {}).get("$ref") or spec.get("$ref")
                    _ = nested  # (kept below)
                    if nested:
                        inner = defs.get(nested.rsplit("/", 1)[-1])
                        if inner is not None:
                            SCHEMA_PARAMS[f"{want}.{field}"] = inner
                            variants = inner.get("oneOf") or inner.get("anyOf") or []
                            print(f"          {field} -> {nested.rsplit('/', 1)[-1]}:")
                            if variants:
                                for v in variants[:4]:
                                    vp = list((v.get("properties") or {}).keys())
                                    vr = v.get("required", [])
                                    print(f"              variant required={vr} accepts={vp}")
                                    for rf in vr:
                                        sp = (v.get("properties") or {}).get(rf, {})
                                        if "const" in sp or "enum" in sp:
                                            print(
                                                f"                {rf} = {json.dumps(sp.get('const', sp.get('enum')))}"
                                            )
                            else:
                                print(
                                    f"              required={inner.get('required', [])} "
                                    f"accepts={list((inner.get('properties') or {}).keys())}"
                                )
                SCHEMA_PARAMS[want] = target
                # Print the OPTIONAL fields that gate interactivity, resolving
                # their refs. Run 18 blocked with no notifications and these are
                # the only knobs that plausibly cause that.
                for gate in ("approvalPolicy", "sandboxPolicy", "turnTrigger"):
                    gspec = (target.get("properties") or {}).get(gate)
                    if not gspec:
                        continue
                    gref = gspec.get("$ref")
                    ginner = defs.get(gref.rsplit("/", 1)[-1]) if gref else gspec
                    vals = (ginner or {}).get("enum") or (ginner or {}).get("oneOf")
                    print(
                        f"          {gate}: {json.dumps(vals)[:200] if vals else json.dumps(ginner)[:200]}"
                    )
                built = _minimal_params(target, str(Path.home()))
                missing = [f for f in req if f not in built]
                print(f"          -> will send: {json.dumps(built)}")
                if missing:
                    print(
                        f"          -> NO DEFAULT for required {missing}; request will be incomplete"
                    )
        elif len(text) > 2500:
            print(f"  {f.name}: {len(text)} bytes, not dumped")
        with contextlib.suppress(json.JSONDecodeError):
            found.append((f.name, json.loads(text)))
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
    SPAWNED.append(proc)
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
        "--kill-strays",
        action="store_true",
        help="SIGTERM app-servers left behind by earlier runs of this script. They "
        "are identified by our own socket prefix in their command line, so the TUI "
        "and anything you started are never touched.",
    )
    ap.add_argument(
        "--log",
        default=None,
        help="write the COMPLETE run to this file (default ./spike-run.log). Send "
        "this file rather than pasting the tail -- step 0 holds the inventory and "
        "it is the part that scrolls away.",
    )
    ap.add_argument(
        "--fresh",
        action="store_true",
        help="create a NEW thread with thread/start and drive that, instead of an "
        "existing one. This is the question that actually matters for spanreed: it "
        "would own its Codex thread, never hijack a human's. Runs 14-19 never tested "
        "it, because thread/start only ran when thread/list came back empty.",
    )
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
    ap.add_argument(
        "--spawn",
        action="store_true",
        help="force spawning a new app-server even when one is already running. "
        "Only useful for reproducing run 6's lock contention.",
    )
    ap.add_argument(
        "--turn-timeout",
        type=float,
        default=300.0,
        help="seconds to wait for turn/start (default 300). A turn runs a real agent; "
        "the 20s used through run 17 was shorter than any agentic turn, so a working "
        "turn and a refused one produced the same timeout.",
    )
    ap.add_argument("--keep-socket", action="store_true", help="don't delete the socket dir")
    ap.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="seconds to wait per operation (default 90). It was 20, which sat exactly "
        "on the boundary of a measured upstream cost: openai/codex#45246 clocks "
        "thread/list at 20-42s on a host with many unarchived threads. Runs 21-22 "
        "timed out at 20.0s on every call, which was this fuse, not a refusal.",
    )
    args = ap.parse_args()
    install_reaper()

    log_path = Path(args.log) if args.log else Path.cwd() / "spike-run.log"
    tee = Tee(sys.stdout, log_path)
    sys.stdout = tee  # type: ignore[assignment]
    atexit.register(tee.close)

    steps = [
        Step(1, "Does `codex app-server` start under this machine's sign-in?"),
        Step(2, "Can a separate process connect and `initialize`?"),
        Step(3, "Can it see threads — including ones a human has open?"),
        Step(4, "Can it start a turn in a thread someone else is using?"),
        # reworded below when --fresh drives our own thread instead
    ]
    s1, s2, s3, s4 = steps

    # Reachability runs FIRST, before the codex check. It was inside step 0,
    # which is gated behind codex being installed — a network probe that could
    # not run unless an unrelated precondition held, in the step meant to
    # establish what is true before anything else.
    hr("Reachability — the dependency every hanging call shares")
    # This used to open a TCP socket and print "OK". Twenty-three runs reported
    # both hosts reachable in 0.0s while every call that needed them hung.
    # A TLS-inspecting proxy ACCEPTS the connection and then intercepts, so TCP
    # success is not evidence of anything -- it is the probe that cannot fail.
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
        if os.environ.get(name):
            print(f"  {name} = {os.environ[name]}")
            FACTS.append(f"{name} is set — traffic is proxied")
    ca_override = os.environ.get("CODEX_CA_CERTIFICATE")
    print(f"  CODEX_CA_CERTIFICATE: {ca_override or '(unset)'}")
    if not ca_override:
        print("    app-server logs 'using system root certificates because no CA override'")
        print("    on every run. On a network that inspects TLS, that is the setting that")
        print("    would be needed -- see openai/codex#6849.")

    for host, port in (("chatgpt.com", 443), ("api.openai.com", 443)):
        t0 = time.monotonic()
        try:
            sk = socket.create_connection((host, port), timeout=10)
        except Exception as exc:
            line = f"{host} TCP UNREACHABLE after {time.monotonic() - t0:.1f}s — {exc}"
            FACTS.append(line)
            print(f"  {line}")
            continue
        tcp = time.monotonic() - t0
        # The handshake is the part a proxy changes. Its issuer names the proxy.
        try:
            ctx = ssl.create_default_context()
            t1 = time.monotonic()
            with ctx.wrap_socket(sk, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                issuer = {k: v for part in cert.get("issuer", ()) for k, v in part}
                org = issuer.get("organizationName") or issuer.get("commonName") or "?"
                hs = time.monotonic() - t1
                tls.settimeout(10)
                tls.sendall(
                    f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
                )
                t2 = time.monotonic()
                head = tls.recv(200).decode(errors="replace").splitlines()
                status = head[0] if head else "(no response line)"
            line = (
                f"{host} TLS OK: tcp {tcp:.1f}s, handshake {hs:.1f}s, "
                f"HTTP {time.monotonic() - t2:.1f}s -> {status[:40]} | issuer={org}"
            )
            # A cert issued by anything other than a public CA means the
            # connection is being terminated and re-signed in the middle.
            if not any(
                k in org.lower() for k in ("digicert", "let's encrypt", "google", "amazon", "isrg")
            ):
                line += "  <-- NOT a public CA: this connection is being intercepted"
        except ssl.SSLError as exc:
            line = f"{host} TLS FAILED after {time.monotonic() - t0:.1f}s — {exc}"
        except Exception as exc:
            line = f"{host} TLS/HTTP FAILED after {time.monotonic() - t0:.1f}s — {exc!r}"
        finally:
            with contextlib.suppress(OSError):
                sk.close()
        FACTS.append(line)
        print(f"  {line}")
    print("  A TCP connect proves only that something answered on port 443.")
    print("  The handshake and the issuer are what say whether it was OpenAI.")

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
    # The working server does not put its socket in CODEX_HOME. Our own spawned
    # one logs "app-server control socket listening socket_path=/var/folders/..."
    # -- that is TMPDIR. Every run globbed CODEX_HOME only, found nothing, and
    # spawned a second server alongside the one that works. Look where the log
    # says it is.
    tmpdir = Path(os.environ.get("TMPDIR") or "/tmp")
    print(f"\n  Searching TMPDIR for app-server sockets: {tmpdir}")
    found_tmp = 0
    for pattern in ("codex*/**/*.sock", "codex*.sock", "**/app-server*.sock", "**/app.sock"):
        try:
            for sock_file in tmpdir.glob(pattern):
                if SOCK_PREFIX in str(sock_file):
                    continue  # one of ours, from this run or an earlier one
                if sock_file not in existing:
                    existing.append(sock_file)
                    found_tmp += 1
                    print(f"    {sock_file}")
        except OSError as exc:
            print(f"    {pattern}: {exc}")
    print(f"  TMPDIR sockets found: {found_tmp}")
    FACTS.append(f"{found_tmp} app-server socket(s) found in TMPDIR (not CODEX_HOME)")
    live: list[Path] = []
    if existing:
        print("\n  SOCKETS FOUND — probing each, because a socket file outliving its")
        print("  process is indistinguishable from a live one until you connect:")
        for e in existing:
            try:
                t = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                t.settimeout(2.0)
                t.connect(str(e))
                t.close()
                live.append(e)
                print(f"    LIVE   {e}")
            except OSError as exc:
                print(f"    STALE  {e}  ({exc.strerror or exc})")
        if not live:
            print("\n  Every socket is stale — a leftover file, nothing listening.")
            print("  Run 7 connected to this one and got ECONNREFUSED, because step 0")
            print("  claimed 'these belong to processes already running' without probing.")
    else:
        print("\n  No existing sockets under CODEX_HOME.")
        print("  Note what that means: nothing here is serving the TUI's threads, so")
        print("  a spawned server necessarily starts empty. That is consistent with")
        print("  step 3 staying empty after you talked to codex in another terminal.")
    existing = live
    locks = home / "thread-writer-locks"
    if locks.is_dir():
        entries = sorted(locks.iterdir())
        print(f"\n  thread-writer-locks/ — {len(entries)} entry(ies):")
        for f in entries:
            try:
                st = f.stat()
                body = f.read_text(errors="replace").strip()[:120] if f.is_file() else ""
                print(
                    f"    {f.name}  {st.st_size}B  mtime={int(time.time() - st.st_mtime)}s ago  {body}"
                )
            except OSError as exc:
                print(f"    {f.name}: unreadable ({exc})")
        print("    A leftover lock here, with no process holding it, is a CANDIDATE for")
        print("    the thread/list and thread/start hangs — NOT a diagnosis. The last")
        print("    time a lock file was treated as evidence of contention it was wrong.")

    # Reachability. Fifteen runs of tuning parameters, and nobody checked
    # whether this machine can reach OpenAI at all. Everything that hangs needs
    # the network (thread/list's remote sources, turn/start's model call);
    # everything that answers is local (thread/loaded/list, initialize). If
    # these are blocked, no parameter fixes it and the spike has been measuring
    # the network rather than Codex.

    control = home / "app-server-control"
    if control.is_dir():
        print("\n  app-server-control/ — a running server may advertise itself here:")
        entries = sorted(control.iterdir())
        FACTS.append(f"app-server-control/ has {len(entries)} entry(ies)")
        for f in entries:
            try:
                body = f.read_text(errors="replace").strip()
                print(f"    {f.name}: {body[:400]}")
                # A path in here is an advertised endpoint: that is how the
                # running server tells other clients where to reach it.
                for tok in body.replace('"', " ").replace(",", " ").split():
                    cand = Path(tok)
                    if (
                        tok.startswith("/")
                        and cand.exists()
                        and cand.is_socket()
                        and cand not in existing
                    ):
                        existing.append(cand)
                        print(f"      -> advertises a live socket, added: {cand}")
                        FACTS.append(f"app-server-control advertises socket {cand}")
            except OSError as exc:
                print(f"    {f.name}: unreadable ({exc})")

    procs = subprocess.run(["pgrep", "-af", "codex"], capture_output=True, text=True)
    raw = [ln.strip() for ln in (procs.stdout or "").splitlines() if ln.strip()]
    lines: list[str] = []
    strays: list[str] = []
    for ln in raw:
        if ln.isdigit():  # macOS pgrep has no -a; it printed bare pids on run 7
            cmd = subprocess.run(["ps", "-p", ln, "-o", "command="], capture_output=True, text=True)
            ln = f"{ln} {cmd.stdout.strip()}"
        if is_stray_spike_server(ln):
            # A leak from an EARLIER run. Nothing of ours is spawned yet at step
            # 0, so every one of these is a server a previous run failed to kill
            # -- and they all contend on the same CODEX_HOME sqlite.
            #
            # This used to be `if "spike" in ln: continue`, which silently
            # dropped exactly these from the inventory. Twenty runs reported
            # "codex processes running: N" with our own leaked servers excluded
            # from N, while the calls that touch that sqlite got slower and then
            # stopped answering at all.
            strays.append(ln)
            continue
        if "spike" in ln:
            continue
        lines.append(ln)
    print(f"\n  codex processes running: {len(lines)}")
    for ln in lines[:8]:
        print(f"    {ln[:160]}")
    print(f"  LEAKED spanreed-spike servers from earlier runs: {len(strays)}")
    for ln in strays[:10]:
        print(f"    {ln[:160]}")
    if strays:
        FACTS.append(f"{len(strays)} LEAKED spike app-server(s) contending on this CODEX_HOME")
        print("    These hold the same thread_history sqlite this run needs. Re-run with")
        print("    --kill-strays to end them, or kill them by hand. Only processes whose")
        print("    command line contains our own socket prefix are listed, so nothing")
        print("    here is the TUI or anything you started.")
        if args.kill_strays:
            for ln in strays:
                pid = ln.split()[0]
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    print(f"    killed {pid}")
                except (OSError, ValueError) as exc:
                    print(f"    could not kill {pid}: {exc}")
            time.sleep(1.5)
    if lines and not live:
        print("\n  Codex IS running but nothing is listening. If none of those command")
        print("  lines is an `app-server`, that is the answer: the TUI keeps its threads")
        print("  in-process and exposes no socket for another client to reach.")

    print("\n  Config — the three documented reasons these calls hang:")
    probe_config(home)

    print("\n  Sign-in state (shapes and times only, never a credential):")
    probe_auth(home)

    print("\n  Reading the thread store directly, bypassing app-server entirely:")
    probe_state_db(home)

    # ---- step 1: start the server -------------------------------------------
    hr("Step 1 — start app-server on a unix socket")
    auto = existing[0] if (existing and not args.spawn and not args.connect) else None
    if auto:
        print(f"  Using the EXISTING socket {auto} rather than spawning a second server.")
        print("  Run 6 spawned one while two codex processes were already running, and")
        print("  thread/list and thread/start each hung for the full timeout while the")
        print("  server spun on one span. CODEX_HOME holds thread_history_*.sqlite with")
        print("  -wal/-shm and a thread-writer-locks dir, so a third server contending")
        print("  for a lock the live one holds is the obvious candidate. Pass --spawn to")
        print("  force the old behaviour.")
    if args.connect or auto:
        sock_path = Path(args.connect) if args.connect else auto
        assert sock_path is not None
        proc = None
        tmp = None
        if not sock_path.exists():
            s1.no(f"{sock_path} does not exist")
            return report(steps, args)
        s1.ok(f"using existing socket {sock_path} (not spawned by this script)")
        print(f"  {s1.verdict}: {s1.detail}")
    else:
        tmp = Path(tempfile.mkdtemp(prefix=SOCK_PREFIX))
        sock_path = tmp / "app.sock"
        cmd = [codex, "app-server", "--listen", f"unix://{sock_path}"]
        print(f"  $ {' '.join(cmd)}")
        # RUST_LOG: app-server is Rust and said nothing at all on run 2. If it can
        # be made to explain why it hangs up, this is the switch that does it.
        env = {**os.environ, "RUST_LOG": os.environ.get("RUST_LOG", "info")}
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env
        )
        SPAWNED.append(proc)

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
    start_wall = time.monotonic()
    hr("Step 2a — what does Codex say its own initialize looks like?")
    schema = dump_schema(codex)
    if not schema:
        print("  (no schema recovered — the matrix below is still a guess)")

    hr("Step 2b — connect as a second client and initialize")
    client = {"name": "spanreed-spike", "version": "0.0.0"}
    shapes: list[tuple[str, Any]] = [
        # InitializeParams.json declares an `experimentalApi` capability —
        # "Opt into receiving experimental API methods and fields." Run 10 dumped
        # that schema and I read past it. thread/loaded/list works while
        # thread/list and thread/start hang, and "experimental" is a plausible
        # reason for a method to be present but inert. Offered first now.
        (
            "experimental",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"experimentalApi": True},
                "clientInfo": client,
            },
        ),
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
            notify(probe, "initialized", {}, style)
            print("  sent `initialized` — required before any other method is serviced")
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
    # Retry with backoff rather than one shot. Run 9 killed the contention
    # hypothesis — zero codex processes, still timed out — and the log shows why
    # that guess was wrong: startup does online work ("list_models
    # refresh_strategy=online", "fetching remote plugin catalog") and these calls
    # land ~230ms after the socket appears, while it is still in flight. One
    # attempt cannot tell "never works" from "not ready yet". A series can.
    attempt_id = 100
    # Run 12: thread/list has required: [] — {} is a VALID call, so the params
    # hypothesis is dead along with contention, warmup and the experimental gate.
    # What survives is in the ACCEPTS list, which was printed and not read:
    #
    #   'sourceKinds', 'originators', 'useStateDbOnly'
    #
    # A `useStateDbOnly` flag only exists if the default path reads something
    # other than the local DB. The startup log shows
    # remote_control_url=https://chatgpt.com/backend-api/ and a remote plugin
    # catalog fetch, so the candidate is that thread/list consults CLOUD threads
    # by default and that request hangs on this network — which would also
    # explain why thread/loaded/list, purely in-memory, returns in 0s.
    #
    # These are fields the schema names, not invented ones, and the local-only
    # variant is tried FIRST so a pass identifies the cause rather than merely
    # working.
    list_attempts: list[tuple[str, dict[str, Any]]] = []
    if "thread/list" in SCHEMA_PARAMS:
        accepts = (SCHEMA_PARAMS["thread/list"].get("properties") or {}).keys()
        if "useStateDbOnly" in accepts:
            list_attempts.append(("local DB only", {"useStateDbOnly": True}))
        if "limit" in accepts:
            list_attempts.append(("local DB only + limit", {"useStateDbOnly": True, "limit": 5}))
    list_attempts.append(("schema-minimal", {}))

    print("  thread/list attempts, local-first:")
    for label, pr in list_attempts:
        print(f"    {label:24} {json.dumps(pr)}")

    for _mid, method, params in ((2, "thread/loaded/list", {}), (3, "thread/list", None)):
        res = None
        if params is None:  # thread/list: try each schema-named variant once
            for label, pr in list_attempts:
                attempt_id += 1
                t0 = time.monotonic()
                try:
                    res = jsonrpc(sock, buf, method, pr, attempt_id, wire, args.timeout)
                    took = time.monotonic() - t0
                    print(f"  {method:20} [{label}] ANSWERED in {took:.1f}s")
                    FACTS.append(f"{method} [{label}] ANSWERED in {took:.1f}s")
                    break
                except Exception as exc:
                    print(
                        f"  {method:20} [{label}] failed after "
                        f"{time.monotonic() - t0:.1f}s -- {type(exc).__name__}"
                    )
                    if isinstance(exc, TimeoutError):
                        # A client-side timeout does NOT cancel the server-side
                        # work. app-server keeps the slot, and it has only six;
                        # openai/codex#36189 describes exactly this, one slow
                        # call filling the queue until everything behind it
                        # expires. Runs 21-22 fired three list variants and four
                        # thread/start attempts after the first timeout and read
                        # the resulting wall of failures as "not a params
                        # problem" -- which was true, and not for that reason.
                        print(
                            f"  {method:20} STOPPING after a timeout: the server is still "
                            "working on that call"
                        )
                        print(
                            "                       and holds one of ~6 request slots. More "
                            "attempts queue behind it"
                        )
                        print(
                            "                       and fail for that reason alone. See "
                            "openai/codex#36189, #45246."
                        )
                        FACTS.append(
                            f"{method} timed out at {args.timeout:.0f}s — remaining variants "
                            "NOT tried, to avoid filling the server's request queue"
                        )
                        break
            if res is None:
                print(f"  {method:20} no variant answered")
                continue
            ids = extract_ids(res)
            found.extend(i for i in ids if i not in found)
            print(f"  {method:20} {len(ids)} thread(s). Raw: {json.dumps(res)[:140]}")
            continue
        for delay in (0, 5, 10, 20):
            if delay:
                print(f"  {method:20} waiting {delay}s — server may still be warming up")
                time.sleep(delay)
            attempt_id += 1
            t0 = time.monotonic()
            try:
                res = jsonrpc(sock, buf, method, params, attempt_id, wire, args.timeout)
                break
            except Exception as exc:
                print(
                    f"  {method:20} FAILED after {time.monotonic() - t0:.1f}s "
                    f"(t+{time.monotonic() - start_wall:.0f}s since start)  "
                    f"{type(exc).__name__}: {exc}"
                )
                if not isinstance(exc, TimeoutError):
                    break  # a real error, not slowness; retrying will not help
        if res is None:
            continue
        print(f"  {method:20} (t+{time.monotonic() - start_wall:.0f}s)", end=" ")
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
        started = SCHEMA_PARAMS.get("thread/start")
        attempts = [_minimal_params(started, str(Path.home()))] if started else []
        starts = (SCHEMA_PARAMS.get("thread/start", {}).get("properties") or {}).keys()
        if "ephemeral" in starts:
            # Same reasoning: schema-named, and skipping persistence is the
            # cheapest way to find out whether the store is what blocks.
            attempts.insert(0, {"ephemeral": True, "cwd": str(Path.home())})
        attempts += [{"cwd": str(Path.home())}, {}]
        for params in attempts:
            attempt_id += 1
            t0 = time.monotonic()
            try:
                res = jsonrpc(sock, buf, "thread/start", params, attempt_id, wire, args.timeout)
            except Exception as exc:
                print(
                    f"  thread/start {json.dumps(params)[:28]:30} no after "
                    f"{time.monotonic() - t0:.1f}s -- {str(exc)[:70]}"
                )
                if isinstance(exc, TimeoutError):
                    print("  thread/start STOPPING after a timeout — see the note above;")
                    print("  further attempts only queue behind the one still running.")
                    break
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
    if args.fresh:
        s4.question = "Can it create its OWN thread and drive a turn in it?"
        # thread/start previously ran only when thread/list came back empty, so
        # the own-thread path went untested for six runs while every run drove
        # a thread a human had open -- the one case Codex is documented to
        # refuse. Force it.
        print("  --fresh: creating our own thread rather than driving an existing one")
        started = SCHEMA_PARAMS.get("thread/start")
        tries = [_minimal_params(started, str(Path.home()))] if started else []
        tries += [{"cwd": str(Path.home())}, {}]
        fresh_id = None
        for params in tries:
            attempt_id += 1
            try:
                res = jsonrpc(sock, buf, "thread/start", params, attempt_id, wire, args.timeout)
            except Exception as exc:
                print(f"    thread/start {json.dumps(params)[:28]:30} no -- {str(exc)[:60]}")
                if isinstance(exc, TimeoutError):
                    print("    STOPPING: a timed-out call still holds a server slot.")
                    break
                continue
            ids = extract_ids(res)
            print(f"    thread/start {json.dumps(params)[:28]:30} YES -- {json.dumps(res)[:90]}")
            if ids:
                fresh_id = ids[0]
                FACTS.append(f"thread/start created our own thread {fresh_id}")
                break
        if fresh_id is None:
            FACTS.append("thread/start FAILED — could not create even our own thread")
        target = fresh_id
    else:
        target = args.turn or (found[0] if created and found else None)

    if target:
        # A lock held by a live process is the documented reason resume hangs
        # instead of erroring, so name the holder rather than spending the
        # timeout rediscovering it. See the README: this is an upstream bug
        # with issues open against codex itself.
        lockdir = home / "thread-writer-locks"
        hits = sorted(lockdir.glob(f"*{target}*")) if lockdir.is_dir() else []
        if hits:
            for f in hits:
                body = ""
                with contextlib.suppress(OSError):
                    body = f.read_text(errors="replace").strip()[:120]
                print(f"  WRITER LOCK held on this thread: {f.name}  {body}")
            FACTS.append(f"target thread {target} HAS a writer-lock file — expect a refusal/hang")
        elif lockdir.is_dir():
            print(f"  no writer-lock file for this thread in {lockdir}")
            FACTS.append(f"target thread {target} has NO writer-lock file")
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
        user_input = SCHEMA_PARAMS.get("turn/start.input")
        if user_input:
            print("  UserInput schema was resolved — see step 2a for the accepted shape.")
        else:
            print("  UserInput schema NOT resolved; falling back to {'type':'text','text':...},")
            print("  which is an assumption this spike has never verified.")
        prompt = (
            f"This is an automated connectivity probe. Reply with exactly "
            f"{SPIKE_MARKER} and nothing else. Do not use any tools, do not read "
            f"or modify any files, do not run any commands."
        )
        print(f"  thread : {target}")
        print(f"  prompt : {prompt[:80]}...")
        # Run 14: thread/resume timed out and the turn/start behind it timed out
        # too. The server logs the resume span entering and never exiting, so if
        # it handles a connection serially the turn may never have been attempted
        # — making "turns are refused" and "the connection was already wedged"
        # indistinguishable, which is the one distinction this step exists for.
        #
        # turn/start goes FIRST now. The thread id came from thread/list; resume
        # was my assumption about what turn/start needs, never a requirement the
        # schema stated.
        resume_schema = SCHEMA_PARAMS.get("thread/resume")
        if resume_schema:
            acc = list((resume_schema.get("properties") or {}).keys())
            local = [k for k in acc if "statedb" in k.lower() or "local" in k.lower()]
            print(f"  thread/resume accepts: {acc}")
            if local:
                print(f"  -> it has a local-only flag too: {local}")
        # turn/start accepts `model` and `modelProvider`. Startup fetches the
        # model list online; if that never completes, a turn with no explicit
        # model has nothing to run on. models_cache.json in CODEX_HOME holds ids
        # the machine has already seen, so this reads one rather than inventing
        # it — the move that finally worked for thread/list.
        turn_extra: dict[str, Any] = {}
        cache = home / "models_cache.json"
        if cache.is_file():
            try:
                blob = json.loads(cache.read_text())
            except (json.JSONDecodeError, OSError):
                blob = None
            ids: list[str] = []

            def collect(node: Any) -> None:
                if isinstance(node, dict):
                    for k, v in node.items():
                        if k in ("id", "slug", "model") and isinstance(v, str) and v:
                            ids.append(v)
                        else:
                            collect(v)
                elif isinstance(node, list):
                    for item in node:
                        collect(item)

            collect(blob)
            uniq = list(dict.fromkeys(ids))
            print(f"  models_cache.json offers {len(uniq)} id(s): {uniq[:6]}")
            turn_params = SCHEMA_PARAMS.get("turn/start", {}).get("properties") or {}
            if uniq and "model" in turn_params:
                turn_extra["model"] = uniq[0]
                print(f"  passing an explicit model: {uniq[0]}")
        else:
            print("  no models_cache.json — cannot supply an explicit model")

        # turn/start STREAMS turn/* notifications and may not answer until the
        # turn finishes. This client skipped every notification in silence and
        # waited 20s for a matching id — so a turn that was running looked
        # exactly like a turn that was refused, and the evidence that it worked
        # was being discarded by the thing looking for it. Documented upstream:
        # a turn longer than the client timeout desyncs, and an early
        # turn/completed can be dropped before the response registers.
        #
        # So: notifications are printed, and `turn/started` is treated as the
        # answer. Whether the final response arrives is a separate question from
        # whether a foreign process can START a turn, which is what step 4 asks.
        seen_notifications: list[str] = []

        def watch(method: str, params: dict[str, Any]) -> None:
            seen_notifications.append(method)
            detail = json.dumps(params)[:100]
            print(f"    <- {method}  {detail}")

        # Run 18: ZERO notifications in up to 300s, and the turn/start span is
        # entered server-side. A turn that were running would emit turn/started.
        # So it is blocked BEFORE starting — and `approvalPolicy` is in the
        # accepts list. A turn awaiting an approval nobody will give blocks
        # forever and emits nothing, which is exactly this shape.
        turn_props = SCHEMA_PARAMS.get("turn/start", {}).get("properties") or {}
        for field in ("approvalPolicy", "sandboxPolicy"):
            spec = turn_props.get(field)
            if not spec:
                continue
            opts = spec.get("enum") or spec.get("const")
            if not opts and "$ref" in spec:
                print(f"  {field}: {json.dumps(spec)[:120]} (ref — see step 2a)")
                continue
            print(f"  {field} accepts: {opts}")
            if isinstance(opts, list) and opts:
                # Prefer the least interactive value the schema offers.
                pref = next(
                    (
                        o
                        for o in opts
                        if isinstance(o, str)
                        and o.lower()
                        in ("never", "none", "auto", "on-failure", "danger-full-access")
                    ),
                    None,
                )
                if pref:
                    turn_extra[field] = pref
                    print(f"  -> passing {field}={pref} (least interactive the schema offers)")

        # Sequence taken from a WORKING third-party client (kcosr/codex-threads),
        # not from another guess. Three things it does that this script did not:
        #
        #   1. thread/resume with excludeTurns: true. Paginated threads require
        #      it — full-history resume is unavailable — which is why plain
        #      resume hung. `excludeTurns` was in the accepts list I printed.
        #   2. LOAD the thread before turn/start. thread/loaded/list returned 0
        #      every run, so the thread was never loaded in this server; the
        #      documented behaviour is an "unloaded thread error", and that
        #      client resumes and retries once on seeing it.
        #   3. Check `canAcceptDirectInput` on the thread first. That field is
        #      step 4's question expressed as data — an explicit false means the
        #      thread refuses direct input, which is an ANSWER rather than a
        #      timeout.
        print("  loading the thread first: thread/resume with excludeTurns=true")
        loaded = None
        try:
            loaded = jsonrpc(
                sock,
                buf,
                "thread/resume",
                {"threadId": target, "excludeTurns": True},
                4,
                wire,
                args.timeout,
                watch,
            )
            print(f"  thread/resume OK: {json.dumps(loaded)[:200]}")
            FACTS.append("thread/resume with excludeTurns=true SUCCEEDED")
        except Exception as exc:
            print(f"  thread/resume (excludeTurns) failed: {type(exc).__name__}: {exc}")

        if loaded is not None:
            # Look the field up structurally. A substring check on the dumped
            # JSON would be fooled by nesting, by spacing, and by the key
            # appearing inside some unrelated string.
            accepts_input = find_key(loaded, "canAcceptDirectInput")
            if accepts_input is not MISSING:
                print(f"  canAcceptDirectInput = {accepts_input!r}")
                FACTS.append(f"canAcceptDirectInput={accepts_input!r}")
                if accepts_input is False:
                    s4.no(
                        "the thread reports canAcceptDirectInput=false — Codex DECLINES "
                        "direct input on it. That is an answer, not a timeout."
                    )
                    print(f"  {s4.verdict}: {s4.detail}")
                    return report(steps, args, proc, tmp, args.keep_socket)

        print("  now calling turn/start on the loaded thread")
        print(f"  waiting up to {args.turn_timeout:.0f}s and printing every notification")
        turn_t0 = time.monotonic()
        try:
            res = jsonrpc(
                sock,
                buf,
                "turn/start",
                {"threadId": target, "input": [{"type": "text", "text": prompt}], **turn_extra},
                5,
                wire,
                args.turn_timeout,
                watch,
            )
            if created:
                # Do NOT call this a pass. The question is "a thread someone
                # else is using" and this thread is ours. Reporting PASS here
                # would be a verdict claiming more than the run showed — the
                # defect this script exists to avoid, in the script's own
                # output.
                s4.verdict = "PARTIAL"
                s4.detail = (
                    f"turn/start accepted ({json.dumps(res)[:100]}) on a thread THIS SCRIPT "
                    f"created and owns.\n"
                    f"        PARTIAL only for 'join a session a human has open' — that is "
                    f"still unanswered.\n"
                    f"        For a HEADLESS BUS WORKER — a Codex nobody attaches to, driven "
                    f"entirely by\n"
                    f"        spanreed — this IS the answer, and it is yes. Spawn the server, "
                    f"own the thread,\n"
                    f"        turn/start on inbound mail. Nothing above blocks that design."
                )
            else:
                s4.ok(f"turn/start accepted: {json.dumps(res)[:200]}")
        except Exception as exc:
            if seen_notifications:
                # The turn ran. That answers step 4 even without the response.
                s4.ok(
                    f"turn/start did not return within {args.turn_timeout:.0f}s, but the "
                    f"server streamed {len(seen_notifications)} notification(s): "
                    f"{seen_notifications[:6]}. A foreign process STARTED a turn in a "
                    f"thread it does not own — the response arrives when the turn ends, "
                    f"which is a different question."
                )
                FACTS.append(f"turn/start streamed {seen_notifications[:4]} — the turn RAN")
                print(f"  {s4.verdict}: {s4.detail}")
                return report(steps, args, proc, tmp, args.keep_socket)
            first = f"{type(exc).__name__}: {exc} after {time.monotonic() - turn_t0:.0f}s"
            print(f"  turn/start alone failed: {first}")
            print(f"  notifications received while waiting: {len(seen_notifications)}")
            print("  retrying on a FRESH connection, resume first — a wedged connection")
            print("  and a refused turn are otherwise indistinguishable")
            try:
                alt = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                alt.settimeout(args.timeout)
                abuf = ws_handshake(alt) if wire == "ws" else bytearray()
                jsonrpc(alt, abuf, "initialize", {"clientInfo": client}, 1, wire, args.timeout)
                notify(alt, "initialized", {}, wire)
                rp: dict[str, Any] = {"threadId": target}
                if resume_schema and "useStateDbOnly" in (resume_schema.get("properties") or {}):
                    rp["useStateDbOnly"] = True
                print(f"  thread/resume params: {json.dumps(rp)}")
                jsonrpc(alt, abuf, "thread/resume", rp, 20, wire, args.timeout)
                res = jsonrpc(
                    alt,
                    abuf,
                    "turn/start",
                    {"threadId": target, "input": [{"type": "text", "text": prompt}]},
                    21,
                    wire,
                    args.timeout,
                )
                s4.ok(f"turn/start accepted after an explicit resume: {json.dumps(res)[:160]}")
            except Exception as exc2:
                s4.no(
                    f"alone: {first} | with resume on a fresh connection: "
                    f"{type(exc2).__name__}: {exc2}  (A5, or non-owning clients are refused)"
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
            # Normalise rather than parse. An earlier version split on the span
            # structure and produced a dash and a full line for every entry when
            # the real logs did not match the assumed shape — a formatter with
            # its own guess about the input, which is the thing being debugged.
            # Stripping digits collapses timestamps, ids and durations, so
            # repeats of one span share a key whatever the surrounding format.
            key = re.sub(r"\d+", "#", line)[:160]
            if key not in seen:
                seen.add(key)
                infos.append(line)
        for line in warns:
            print(f"  {line[:240]}")
        if warns and infos:
            print(f"  -- {len(infos)} distinct INFO line(s) follow --")
        for line in infos[-12:]:
            print(f"  {line[:200]}")
        total = len(server_output.strip().splitlines())
        print(f"  [{len(warns)} warn/error, {total} lines total; INFO deduped]")
    if FACTS:
        hr("Key facts (repeated here because the top of a long run gets truncated)")
        for line in FACTS:
            print(f"  {line}")
    # Printed unconditionally, including the zero case. "the server asked us
    # nothing" and "we never looked" have to be distinguishable, or the next run
    # re-argues a question this one already settled.
    if SEEN_SERVER_REQUESTS:
        uniq = sorted(set(SEEN_SERVER_REQUESTS))
        print(f"\n  server->client REQUESTS seen ({len(SEEN_SERVER_REQUESTS)} total):")
        for m in uniq:
            print(f"    {m}  x{SEEN_SERVER_REQUESTS.count(m)}")
        print("    Each was answered. An unanswered one is the documented cause of")
        print("    a span that stays open with no error -- which is what hung before.")
    else:
        print("\n  server->client REQUESTS seen: NONE.")
        print("    The client answered everything asked of it, so an unanswered server")
        print("    request is RULED OUT as the cause of any hang in this run.")
    hr("Result")
    for s in steps:
        print(f"  {s.n}. [{s.verdict:11}] {s.question}\n        {s.detail}")

    verdicts = {s.n: s.verdict for s in steps}
    print()
    if verdicts[4] == "PARTIAL":
        print("  Two designs, and this result answers them differently:")
        print()
        print("  HEADLESS BUS WORKER (a Codex nobody attaches to)            ANSWERED: YES")
        print("    Spawn app-server, create a thread, turn/start on inbound mail.")
        print("    Every step of that just ran. Nothing here blocks it.")
        print()
        print("  JOIN A HUMAN'S OPEN SESSION                                 STILL NO")
        print("    Needs a thread this script did not create. On 0.154.0 the TUI")
        print("    exposes no socket and locks the thread store, so there is nothing")
        print("    to join. See the README.")
    elif verdicts[4] == "PASS":
        if getattr(args, "fresh", False):
            # Do not let a pass on OUR OWN thread print as a pass on a human's.
            # The two are different questions and Codex answers them differently:
            # it holds a per-thread writer lock, so the second one is the one it
            # refuses. Saying "a non-owning process can drive an open thread"
            # here would be the spike lying about its own scope.
            print("  A separate process can create a Codex thread and drive a turn in it.")
            print("  That is the case spanreed actually needs: it would OWN its thread.")
            print("  This says NOTHING about driving a thread a human has open — for that,")
            print("  re-run with --turn <id>. Expect a refusal: Codex takes a writer lock")
            print("  per thread, and a thread open in another client is already claimed.")
        else:
            print("  A non-owning process can start a turn in a thread a human has open.")
            print(f"  CHECK THE HUMAN'S CODEX WINDOW: did it show a turn replying {SPIKE_MARKER}?")
            print("  If it did, a Codex session can be a real peer on the bus, not a mailbox.")
            print("  If the turn ran but the human saw nothing, that is the ANSWER TO A")
            print("  DIFFERENT QUESTION and worth reporting separately — it would mean turns")
            print("  are possible but invisible, which is not good enough.")
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

    out = sys.stdout
    if isinstance(out, Tee):
        out.flush()
        print(f"\n  FULL LOG WRITTEN TO: {out.fh.name}")
        print("  Send that file. It has step 0 — sockets, config, processes — which is")
        print("  where the answer has been sitting, unreadable, for several runs.")

    return 0 if verdicts[4] in ("PASS", "PARTIAL") else 1


if __name__ == "__main__":
    sys.exit(main())
