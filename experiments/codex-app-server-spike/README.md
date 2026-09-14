# Codex app-server spike

**One question:** can a second process start a turn in a Codex session someone
else has open?

If yes, spanreed can treat a Codex session as a real peer — mail arrives, the
monitor calls `turn/start`, the agent wakes with the human watching. If no, a
Codex agent is a mailbox read at hook boundaries, and the integration is worth
much less. Everything else about the design follows from this answer.

## Run it

On a machine with Codex signed in:

```sh
python3 spike.py                     # steps 1-3, read-only
python3 spike.py --turn <thread-id>  # all four; starts ONE short turn
```

Stdlib only, installs nothing, touches no repo. The sole side effect is one turn
in a thread you nominate, with a prompt that asks for a fixed string and forbids
tools and file access.

**Steps 1-3 passing does not answer the question.** They show you can connect and
look. `--turn` is the test.

## Why it is written this way

It was written on a machine with no Codex, against documentation that could not
be executed — and against an interface OpenAI's own docs call *"experimental and
not supported for production workloads"*. So every step is an assumption, and the
script's real job is to tell you **which** assumption broke:

```
A1  `codex app-server --listen unix://PATH` is the right invocation      CONFIRMED
A2  the unix transport frames messages as newline-delimited JSON        WRONG — see below
A3  `initialize` is required first, per connection                      CONFIRMED (via stdio)
A4  method names are thread/list, thread/loaded/list, turn/start
A5  turn/start takes a threadId and an input message
```

## What the real runs found

**The assumption that broke was not on the list.** Three runs against
codex-cli 0.154.0:

| run | result |
|---|---|
| 1 | step 1 PASS; `initialize` closed the connection, no server output |
| 2 | 8 framing x params combinations, all closed; still no output |
| 3 | `RUST_LOG=info` made it speak, and a stdio control answered |

Run 3's log:

```
WARN codex_app_server_transport::transport::unix_socket:
     failed to upgrade control socket websocket connection:
     WebSocket protocol error: httparse error: invalid token
```

**The unix socket is a WebSocket control socket.** Raw JSON was being parsed as
an HTTP request line. A2 was wrong in a way none of its alternatives covered —
the matrix tried JSONL and LSP framing and the answer was neither.

The stdio control settled the rest: `initialize` with `clientInfo` is correct,
and the server echoed the client name back. So the params were right from run 1;
only the transport was wrong.

A failure naming A1–A5 means **this script guessed wrong**. A clean protocol
error from the server means **Codex declines to do it**. Those are different
answers and the output keeps them apart.

Every step prints PASS / FAIL / SKIP / DID NOT RUN with a reason. A step that
could not run says so rather than printing nothing — "no threads found" when you
never connected is worse than a crash.

## What was verified before shipping it

The Codex side could not be. The script's own logic was, against a stub
app-server:

| case | result |
|---|---|
| no `codex` on PATH | step 1 FAIL, steps 2-4 `DID NOT RUN`, exit 1 |
| full run, steps 1-3 | PASS, step 4 SKIP with the caveat, exit 1 |
| `turn/start` accepted | step 4 PASS, exit 0 |
| `turn/start` refused by server | step 4 FAIL naming the server's error, exit 1 |

The stub interleaves a notification and a response to a different id before each
real reply, so the client's id-filtering is exercised rather than assumed.

## Reporting back

The verdict table is the output. Worth capturing separately if it happens:
**a turn that runs but the human never sees** — that would mean turns are
possible but invisible, which is a different answer from either yes or no.


---

# ANSWER: no, not today. Eight runs, codex-cli 0.154.0, macOS.

**A Codex TUI session cannot be reached by another process.** Not via IPC —
nothing is listening. Not via a second app-server — it is locked out of the
thread store while the TUI holds it.

## The evidence

| finding | how it was established |
|---|---|
| the TUI exposes no socket | `~/.codex/ipc/ipc.sock` exists but is **stale** — ECONNREFUSED on probe |
| nothing advertises a server | `app-server-control/` holds one empty `app-server-startup.lock` |
| no app-server is running | the two codex processes are `codex` (the TUI) and a ChatGPT.app computer-use helper |
| ~~a second server is locked out~~ | **RETRACTED — this was wrong.** Run 9 had `codex processes running: 0` and `thread/list` still timed out. The lock-contention story was inferred from a `thread-writer-locks/` directory and a coincidence of timing, asserted here as measured, and then falsified. The real candidate is startup: the log shows `list_models{refresh_strategy=online}` and `fetching remote plugin catalog` still running when the call lands ~230ms later. Untested as of writing. |
| a human's thread never appears | talking to `codex` in another terminal added nothing to `thread/list` |

**Row 4 was the one I was most confident about and it is the one that was
wrong.** The "contrast" — fast without a TUI, hung with one — came from
comparing runs that differed in more than the TUI, and I read a correlation
across two runs as a demonstrated mechanism. Run 9 held the TUI at zero and the
timeout persisted.

The finding survives without it: rows 1-3 and 5 still say the TUI exposes
nothing and a human's thread never appears. What does not survive is the
explanation of *why* the second server is useless, which is now open.

## What was established, and is reusable

The protocol work is sound and none of it is wasted if this changes:

- `codex app-server --listen unix://PATH` runs under **ChatGPT subscription
  sign-in** — no API key
- the unix transport is a **WebSocket control socket**, not newline JSON. There
  is a working stdlib RFC 6455 client here, tested against `aiohttp`
- `initialize` from a foreign process **works** — the server echoes the client
  name back and logs the call under `rpc.transport="unix_socket"`
- `thread/loaded/list` works; the full protocol schema is emitted by
  `codex app-server generate-json-schema --out DIR` (305 files)

So the client is finished. What is missing is a server that owns a human's
session and will talk to anyone else.

## What would change the answer

1. **OpenAI ships the wake primitive.** Four open issues ask for exactly it —
   [#20312](https://github.com/openai/codex/issues/20312),
   [#35542](https://github.com/openai/codex/issues/35542),
   [#8375](https://github.com/openai/codex/issues/8375),
   [#29922](https://github.com/openai/codex/issues/29922). #35542 describes this
   situation precisely: *"nothing can reach an idle TUI"*.
2. **Use the App or VS Code extension instead of the TUI.** Both reportedly
   register as thread owners on the IPC router, which is why that socket exists
   at all. Untested here, and it means not working in a terminal.

## Why it stopped here

Eight runs, and most of the cost was self-inflicted: five spawned a private
server that could never have seen another process's threads, and one connected
to a dead socket on the strength of a sentence in this script that claimed
sockets were live without probing them.

What the script got right is that every one of those was reported as **the
script being wrong**, never as Codex refusing. That distinction is why the
conclusion above can be trusted: the failures that were mine were labelled mine,
and the one that is Codex's is the only one left.
