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

# SUPERSEDED — see "The cause, found on run 13" at the end. The conclusion below was drawn while the client was still mis-calling the server.

# ~~ANSWER: no, not today.~~ Eight runs, codex-cli 0.154.0, macOS.

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


---

# Run 22: the fuse was the bug, and every extra attempt made it worse

Run 22 cleared the remaining environmental theories in one pass:

- **The store is fine.** Read directly with `immutable=1`: `thread_turns` 36 rows,
  `thread_items` 201, `thread_history_projection_state` 2. Instant, unlocked.
- **No leaked servers.** The stray count was zero.
- **No server requests.** Ruled out in run 21 and still true.
- **Network reachable**, both hosts, under 0.1s.

And `thread/list` failed on all three variants at **exactly 20.0s** — which is
this script's `--timeout` default, not a property of Codex.

## The measured cost is 20-42s; the fuse was 20

[openai/codex#45246](https://github.com/openai/codex/issues/45246) clocks
`thread/list` at **20-42 seconds** on a host with many unarchived threads — it
scales with thread count. [#36416](https://github.com/openai/codex/issues/36416)
reports the same shape when the call scans rollouts.

Runs 1-20 sat under that line and got 0.0s answers. The store then grew — 201
items, largely from these runs — and crossed it. Nothing about the protocol
changed between run 20 and run 21. The DB got bigger and the fuse stayed at 20.

Default is now **90s**, above the reported ceiling.

## Why the failures after the first one meant nothing

A client-side timeout does **not** cancel the server-side work. app-server keeps
the slot, and it has about six;
[#36189](https://github.com/openai/codex/issues/36189) describes one slow call
filling the queue until everything behind it expires.

Runs 21 and 22 fired three `thread/list` variants and four `thread/start`
attempts *after* the first timeout — seven requests into six slots, each queued
behind a call still running. The script then printed:

```
thread/list  every variant failed — not a params problem
```

which is true, and not for the reason it implies. The variants were never
reached. That line is now `no variant answered`, and a timeout stops the loop
with the queue explained, in all three places that used to keep going.

Verified both directions against a stub with a deliberately slow `thread/list`:
a fuse below the server's cost stops after one attempt instead of seven; a fuse
above it answers normally.

## What this retracts

The run-19 heading and the run-20 heading both named a cause that later runs
disproved. They are kept, marked, because the sequence is the point — four
plausible mechanisms (writer lock, unanswered server request, leaked servers,
DB contention) each explained the evidence available when it was proposed, and
each was killed by a cheap probe rather than by argument. The one that survived
was visible in every run since run 14: the timeouts all landed on the same
round number, and a round number is a fuse, not a finding.

# Run 21: the server-request theory is dead, and the spike was hiding its own mess

Run 21 answered the previous run's question cleanly, in the negative:

```
server->client REQUESTS seen: NONE.
```

app-server asked this client nothing. Unanswered server requests are **ruled
out**. The responder stays — it is correct, and four other clients needed it —
but it is not what hangs this spike.

Run 21 also produced a regression that is more informative than the theory it
killed. `thread/list` with `useStateDbOnly: true` **answered in 0.0s in every
earlier run and now times out**. That call is the one documented to stay local.
The protocol did not change between runs; the machine did.

## What the spike was hiding

The process inventory contained this:

```python
if "spike" in ln:
    continue
```

Intended to skip the current run. It also skipped **every app-server leaked by
the twenty runs before it**. Each run spawns a server against the same
`CODEX_HOME`, cleanup lived only at the end of `report()`, and any Ctrl-C or
exception skipped it. So the inventory printed "codex processes running: N"
with our own survivors excluded from N — while the calls that touch that store
got slower, and then stopped answering.

Three fixes, in order of how much trouble they were:

1. **Leaked servers are counted and named**, not filtered out.
2. **`install_reaper()`** terminates spawned servers from `atexit` and from
   SIGINT/SIGTERM/SIGHUP, so the paths that skip `report()` no longer leak.
   Verified by sending SIGINT mid-run and counting survivors: zero.
3. **`--kill-strays`** ends the ones already out there.

### The bug inside the fix

The first version of the detector tested `if "spanreed-spike" in ps_line`. Run
against a test harness, it reported **3** strays where 1 existed — it had matched
the shell running the test, whose command line merely quoted the string. With
`--kill-strays` that is a SIGTERM to the user's shell.

`is_stray_spike_server()` now requires all three of: argv[0] whose basename is
`codex`, `app-server` in the arguments, and our own socket prefix. A shell
quoting any of those fails the first test. Six cases are asserted, including the
exact line that fooled the first version.

The same bug then bit the test harness itself — `pkill -f "sleep 999"` killed
the shell running it, for the identical reason. Substring is not identity, in
either direction.

## Reading the store without app-server in the way

`probe_state_db()` opens `thread_history*.sqlite` directly with `immutable=1`,
which skips locking entirely so the probe cannot itself become the contention it
is looking for. If the file reads fine here while app-server cannot answer from
it, the fault is in the server or in contention for the file — not in the
protocol, and not in this client.

# Run 20: the client was never holding up its end (run 21 ruled this out as the cause — read on)

Run 20 killed the writer-lock theory in one line: `thread/start` **also** timed
out. A thread that does not exist yet cannot be locked. The lock is real and
documented, but it is not what hangs this spike.

What the 20 runs actually show is a clean split:

| Answers instantly | Hangs forever, span open, no error |
| --- | --- |
| `initialize` | `thread/list` without `useStateDbOnly` |
| `thread/loaded/list` | `thread/resume` |
| `thread/list` with `useStateDbOnly: true` | `thread/start` |
| | `turn/start` |

The right-hand column is every call that reaches the backend. The left-hand
column is every call that does not.

## The cause: app-server asks the client questions, and this client never answered

app-server is **bidirectional** JSON-RPC. It sends *requests* to the client —
`execCommandApproval`, `applyPatchApproval`, `mcpServer/elicitation/request` —
and blocks until the client responds. There is no timeout on the server side.

This spike's `jsonrpc()` matched inbound frames by `id` and dropped everything
else. A server request has a `method` *and* an `id`, so it fell through to the
notification branch and was discarded — and `on_notify` was only ever wired up
during `turn/start`, so during `thread/start` the discard was silent.

At least four other clients shipped this same bug:

- [Clubhouse#1720](https://github.com/Agent-Clubhouse/Clubhouse/issues/1720) — "drops 7 of 9 server requests with no response, hanging the turn"
- [signalxjs/ai#126](https://github.com/signalxjs/ai/issues/126) — "no default response, so an unmapped request leaves the thread hanging"
- [solenta#1171](https://github.com/currentbits/solenta/issues/1171) — "left unanswered never times out; the thread stays in waitingOnApproval indefinitely"
- [memql-cockpit#446](https://github.com/znasllc-io/memql-cockpit/issues/446) — a framing variant of the same stall

Their recommended fix is the one taken here: answer *everything*, and answer
unknown methods with `-32601` rather than silence, because a refusal is
diagnosable and a stall is not.

## What changed

`handle_server_request()` replies to every server-initiated request on every
call, not just during turns: approvals get `decision=decline`, elicitation gets
`action=decline`, anything unrecognised gets `-32601`. Declining is correct for a
probe whose prompt tells the model to touch nothing — it ends the turn with an
answer instead of a hang.

Per the app-server protocol the `"jsonrpc": "2.0"` member is **omitted** on
replies; app-server leaves it off its own frames.

Every server request is printed as it arrives, and the report prints the
**zero case explicitly** — "REQUESTS seen: NONE" rules the theory out, where
silence would leave the next run re-arguing it.

Verified against a stub that reproduces the reported bug: a server that blocks
`thread/start` until the client answers now gets its answer and completes, where
before it timed out exactly as run 20 did.

# Run 19: a writer lock looked like the cause (run 20 disproved this — read on)

Run 19 added `thread/resume` with `excludeTurns: true` and it **also** timed out.
The server log ends on the `thread/resume` span with 0 warn/error — entered,
never returned. Same shape as `turn/start`, one call earlier.

The cause is not in this script. Codex takes a **per-thread writer lock** at
`$CODEX_HOME/thread-writer-locks/<thread>.lock`. A thread another client has open
is already claimed, and resume blocks on the lock rather than failing, so the
symptom is a hang with no error. This is a known upstream problem with issues
open against codex itself and against several third-party clients:

- openai/codex#44449 — threads viewed in the iOS app stay locked in the daemon; desktop fails with "already has an active writer"
- openai/codex#40973 — remote cannot open a VS Code-owned thread; the writer stays held after the other client disconnects
- manaflow-ai/cmux#11973, pingdotgg/t3code#8259, getpaseo/paseo#3573 — the same error from three unrelated clients

So `--turn <a human's thread>` was testing the one case Codex is documented to
refuse.

## The question the spike never asked

`thread/start` ran **only when `thread/list` came back empty**. Once step 3 began
returning a real thread, the own-thread path stopped running entirely — six runs
drove a locked thread and none created one.

That inverted the priority. Spanreed does not need to hijack a human's Codex
window; it would **own** its thread, the way it owns a Claude session. `--fresh`
forces that path:

```sh
python3 spike.py --fresh                 # our own thread -- the case that matters
python3 spike.py --turn <human-thread>   # the hijack -- expect a writer-lock refusal
```

Step 4 renames itself under `--fresh`, and the pass text no longer tells you to
go look at the human's Codex window — a pass on our own thread is not a pass on
someone else's, and the two must not print the same.

The target's lock file is now checked before the attempt, so a held lock is named
up front instead of rediscovered by spending the timeout.

# The cause of the run-18 hang, found by reading a working client

Runs 14-18 had `turn/start` hang with zero notifications. I burned five runs
guessing parameters (`model`, `approvalPolicy`, `sandboxPolicy`). The answer was
not a parameter. `kcosr/codex-threads` is a third-party client that drives
app-server threads successfully, and its documented behaviour names all three
mistakes:

| What it does | What this spike did | Why it mattered |
| --- | --- | --- |
| `thread/resume` with `excludeTurns: true` | resumed without it | paginated threads *require* it; full-history resume is unavailable, so plain resume hangs |
| loads the thread before driving it | never loaded it | `thread/loaded/list` returned **0** every run; app-server has an "unloaded thread error" for exactly this, and that client resumes-and-retries once on seeing it |
| reads `canAcceptDirectInput` first | never read it | an explicit `false` is step 4's answer as *data* — a refusal, not a timeout |

The signal was in the output the whole time: `thread/loaded/list` returned 0 on
every single run, sitting next to a `thread/list` that returned a real thread. A
thread that exists but is not loaded is the documented failure mode, and I read
past it because I was looking at `turn/start`'s parameters instead.

The same client waits **up to an hour** for a turn to reach terminal status, so a
300s timeout was never on its own evidence of anything.

# The cause, found on run 13

**`thread/list` consults remote sources by default, and that request never
returns on this network.** Passing the flag the schema already named fixes it:

```
thread/list [local DB only]  {"useStateDbOnly": true}   ANSWERED in 0.0s
thread/list [default]        {}                          4 timeouts, to t+115s
```

Same run, same server, same connection. The contrast is the measurement.

And it returned a thread **this script did not create** —
`01a0a1a9-7892-7292-b341-81998259f405`, persisted from an earlier session. So
step 3 is a real pass, not a self-created substitute.

## What this retracts

Everything above about the TUI exposing nothing is **suspect and probably
wrong**. Every attempt to enumerate a human's threads was made with a call that
could not return, so "no threads visible" was never evidence about visibility.
The earlier conclusion should not be cited until re-tested.

## The wrong answers, in order

Each was stated with more confidence than it had earned, and each was
falsified by the next run:

| # | explanation | killed by |
|---|---|---|
| 1 | sqlite lock contention with the TUI | zero codex processes, still hung |
| 2 | startup warmup / network fetches in flight | retries to t+115s |
| 3 | missing `initialized` handshake | real, and necessary — but not sufficient |
| 4 | `experimentalApi` gate | accepted, hang persisted |
| 5 | wrong/missing params | `required: []` — `{}` was always valid |
| 6 | **remote lookups on a blocked network** | **confirmed: the flag fixes it** |

Five wrong, one right. The right one came from reading the `accepts` list the
script had been printing for two runs — `sourceKinds`, `originators`,
`useStateDbOnly` — rather than from reasoning about the logs.

## The transferable part

The script's value was never its correctness. It was wrong repeatedly and in
ways that produced confident, plausible failure reports. What made the answer
reachable is that every failure named **which assumption** it implicated, so a
wrong guess cost one run instead of becoming the conclusion.

The thing that actually ended it was going back to primary sources — the docs
for the handshake, and the machine's own schema for the parameters. Both had
been available from the first run. Nine runs of inference from logs produced
five wrong answers; two readings of the spec produced the two right ones.
