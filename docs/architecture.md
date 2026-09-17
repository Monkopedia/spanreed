# Architecture

## Problem

A developer running multiple Claude Code sessions — one per active repo — has no way for those sessions to coordinate. Workflows that span repos (PR review follow-up, cross-repo refactors, "tell the other agent I'm done") need a coordination layer.

Existing options surveyed and rejected:

- **Anthropic's Agent Teams** (experimental, ships with Claude Code): close to what we want, but only top-down — teammates must be spawned by a lead. Independently-launched sessions can't self-register into a peer mesh.
- **Conductor / Crystal / claude-squad**: UI multiplexers. Switch between sessions, no inter-agent messaging.
- **Google's A2A protocol**: cross-vendor, but no Claude Code adapter exists.
- **Ruflo**: heavyweight opinionated swarm framework — adopt the whole worldview or nothing.

Nothing fits "many independently-launched Claude Code instances + lightweight self-registration + direct peer-to-peer messaging."

## Decomposition

Two layers, each playing to its strength.

### Layer 1: MCP server (the API)

A small local daemon exposing MCP tools:

| Tool | Purpose |
|---|---|
| `register_agent(name, working_dir)` | Join the bus, get an `agent_id` |
| `deregister_agent(agent_id)` | Leave cleanly |
| `list_agents()` | Discover peers |
| `send_message(to_agent_id, body)` | Post to recipient's inbox |
| `recv_messages(agent_id)` | Drain own inbox |

State persists under `~/.claude/spanreed/`:
- `registry.json` — currently registered agents and metadata
- `inboxes/<agent_id>.jsonl` — per-agent append-only message log

This is the right shape for MCP — request/response tool calls map cleanly to bus operations.

### Layer 2: Plugin Monitor (the delivery mechanism)

MCP alone can't *push* to Claude — she'd have to poll, which doesn't fire when she's idle. Claude Code's [Monitor](https://code.claude.com/docs/en/plugins#add-background-monitors-to-your-plugin) primitive (background process whose stdout lines surface as notifications) IS the push channel.

The plugin's Monitor:
1. Runs for the lifetime of the session as a background process.
2. Watches the local agent's inbox for new entries (file watcher, polled tail, or pubsub from the MCP daemon).
3. When a new message lands, emits a signal line to stdout.
4. Claude sees the signal as a notification, then — steered by the monitor's description — calls `recv_messages` via MCP to fetch and decide what to do.

The plugin also runs:
- A `SessionStart` hook to register the agent into the bus.
- A `Stop` hook (or PID-based liveness in the daemon) to deregister.

**Liveness model.** Presence is PID-based, not heartbeat-based: an agent is live iff its registered PID is alive *and* that PID's start-time matches what was captured at registration. The start-time match is what makes "trust the PID" safe — it catches the case where the agent died and the OS recycled its PID onto an unrelated process. We deliberately avoid a `last_seen` TTL / timer-driven heartbeat: waking idle agents just to refresh a timestamp is wasteful, and a quiet-but-alive agent must never be reported as gone. (Start-time is unavailable without `/proc`, e.g. on macOS; there we fall back to a bare PID-alive check and accept the small reuse risk.)

## Trust model

Critical and non-obvious. Three distinct trust levels in play:

| Source | Trust level | Use |
|---|---|---|
| Monitor `description` (plugin manifest) | **Trusted** — set by plugin author, baked into context | The *policy*: how to interpret signals, what to do when one arrives |
| Monitor `stdout` line (the signal) | **Untrusted** — could be poisoned upstream | Just a *poke* ("you have mail"). Should not contain executable instructions. |
| Message body fetched via `recv_messages` | **Untrusted data** | Claude reads, applies judgment, decides whether and how to act. |

**Sender identity is NOT authenticated, deliberately.** The three levels above are all
about *body content*; this row is about *who a message claims to be from*. `from_agent` is a
parameter supplied by the caller and is verified against nothing — not the calling process,
not the registry.

Asked *"should `from_agent` be authenticated — derive it from the calling process, validate
it against the registry, or leave it as is?"*, the owner answered (2026-08-18):

> *"no, if its on the bus you can trust it"*

The question and answer are also recorded on `Monkopedia/spanreed#16`, dated and written at
the time. That is a second location for the same claim by the same author, not independent
corroboration — nothing in this repo can corroborate a session transcript.

So a forged sender is not a threat this design defends against. The boundary that carries
that trust is the same one stated under *Scope: interactive mode only* (which lists
inter-agent authentication as an explicit non-goal) and *Cross-host: the SSH bus-bridge* —
**the single-user assumption: "you can reach the box"** — and it reaches *further than one
machine*, because `spanreed conjoin`
bridges a peer's bus over SSH and the receiving side appends peer frames verbatim
(`bridge.py`'s reader, on the `msg` frame). A bridged agent is not co-located.
`open-questions.md` states the same boundary for the bridge explicitly. **If that assumption ever stops holding, this row is the
first thing to revisit** — the ruling is scoped to it.

Two things the ruling does not extend to, because a one-sentence answer is scoped by the
question it answered:

- **It is about the transport, not about authority.** A trusted channel still does not turn
  one agent's assertion about the owner's intent into the owner's decision. Message bodies
  remain *untrusted data* per the table above, and that is unchanged.
- **Trusting the channel is not the address being right.** An id can still be wrong by
  *accident* — a session that changed directory recomputes a different one — and a reply
  addressed to a wrong id goes nowhere silently. Trusting the bus does not make a
  misaddressed message arrive.
- **It cannot establish its own premise.** This ruling was given by the owner *directly, in
  a session* — not over the bus. That is load-bearing: a copy of it **arriving over the bus**
  could not establish the bus's trustworthiness without circularity. A relayed copy is
  evidence the ruling exists; it is not authority to act on it. An agent that has only heard
  it relayed **over the bus** has not received it. (This document is of course itself
  second-hand — it records the ruling, it is not the delivery of it.)
- **It cannot reconfigure a session.** The channel can carry a request, a dispatch, or an
  approval of *work*. It cannot switch on a capability a session's own operating
  instructions disable — that is a property of the session, not of the message, and no
  quote however genuine reaches it. An agent whose instructions disable subagents does not
  gain them by being told the bus is trusted.

This separation surfaced empirically from test #1: when a signal carried embedded instructions ("reply ACK"), Claude correctly refused — monitor stdout isn't a trusted command channel. The fix isn't to defeat the defense, it's to put policy in the description (trusted) and treat message content as data (not commands).

## Escalation: PushNotification

Discovered in test #3: Claude can call the `PushNotification` tool to alert the human when something needs attention. The harness automatically suppresses the notification when the user is active in the receiving terminal — so worker agents can flag without spamming. The bus policy should bundle this in: when a response status is `needs-user-attention`, also call `PushNotification` so the human gets pulled in even if they're heads-down in a different terminal.

## Agent status (attention-level reporting)

`PushNotification` is the *push* escalation. `status` is the complementary *pull* signal: a self-reported, four-level read on how much human attention an agent needs — `idle` / `working` / `needs_input` / `blocked` — so a human or peer scanning `list_agents` can see at a glance who's stuck without anyone being interrupted. Wire-format and the full semantics are in [`protocol.md`](protocol.md#status); the design choices worth recording here:

- **It's a sibling of `focus`, not new infrastructure.** Same shape: a self-set field on the registry `Agent` record, set via `set_status`, surfaced in `list_agents`. That reuse is deliberate — it kept the feature small and means it inherits focus's properties (per-agent, filesystem-backed, no daemon).
- **Pull, not push — by design.** Setting status never notifies a peer (no monitor event, no inbox message). A status change waking every peer would be the opposite of the point. You learn a peer's status when you already call `list_agents` (for free — it rides the discovery call you make anyway), so querying costs nothing extra.
- **Reset on re-registration** (unlike `focus`, which is preserved). A fresh session isn't `blocked` because the last one was; carrying a stale status across a restart would actively mislead the fleet view this exists to provide.
- **`idle` is best-effort.** An agent goes idle exactly when it stops running, so it can't reliably announce the transition (there's no Stop hook). The human-need levels are the reliable, load-bearing part — the agent declares them *while active*, which is when they matter. We accepted this rather than add a Stop hook for v1.
- **Token-minimal and opt-in.** The real cost of the feature isn't the one short enum per agent in `list_agents` — it's the per-session instruction teaching agents to maintain status. So it's gated by a bus-wide `status_tracking` flag (off by default): `session-start` injects the instruction only when enabled, so a bus that doesn't want the feature pays zero context tokens for it. The flag gates only the *instruction*; the `set_status` tool itself always works.

## Activity log (presence history)

`focus` and `status` are *current-state* fields — the registry only ever holds the latest value. The activity log adds the *history*: an opt-in, append-only timeline of focus/status transitions, motivated by a concrete reader — dump it and pipe it into an LLM for a daily "what did my agents do" digest. Wire-format in [`protocol.md`](protocol.md#activity-log); the design choices worth recording:

- **There's a named reader, or it doesn't get built.** A log without a consumer rots. This one exists specifically to feed a summarizer; that's what justifies the category shift from current-state to history.
- **Spanreed emits, it doesn't summarize.** The bus writes `activity-log.jsonl` and `spanreed log` dumps it; the LLM step is the caller's pipe. Baking an LLM dependency into the bus would be scope creep — the user said "pass it into haiku or something" and owns that step.
- **Opt-in, zero-cost-when-off**, same `config.json` flag pattern as `status_tracking`. The write piggybacks on `set_focus`/`set_status` (one extra append on a write that already happens), and only on a *genuine* change — a no-op re-set isn't logged, so the log is transitions, not noise. This pairs with the softening of focus to major-task granularity: a sparse log of real transitions is exactly the right grain for a digest.
- **Single fleet-wide file, human-read-only.** One `activity-log.jsonl` (not per-agent) so a whole-fleet digest is one `cat`/pipe. Deliberately a CLI read, *not* an MCP tool — it's for the human to review, and exposing it to agents would re-introduce the very per-query token cost the pull-only design avoids elsewhere.

## Scope: interactive mode only

Background / headless workers have other handles — Claude Code's Remote Control, `claude -p`, scheduled triggers. Spanreed targets the case where a human is actively using a Claude session and another agent needs to interject.

Out-of-scope explicitly:
- Persistence across machine reboots
- Authentication / authorization between agents (single-user assumption)

(Cross-host messaging was previously out-of-scope; it is now an in-design feature — see below.)

(So were headless workers, in the first line of this section. A **Codex worker** is one: no human attached, woken only by mail. That is now an in-design feature too — see "Codex workers" below. The reason the original exclusion held was that headless agents had other handles and did not need the bus; a Codex session has no such handle, which is exactly why it needs one.)

## Codex workers

A **Codex worker** is a bus agent with no human attached: a long-lived process that owns one
`codex app-server` thread and turns inbound mail into turns. Feasibility is settled empirically —
see [findings.md](findings.md#test-5-can-a-foreign-process-drive-a-codex-turn-2026-09-15) — and this
section is the design, not the evidence.

```
spanreed codex --name reviewer --cwd ~/git/foo \
               --model gpt-5.6-sol --effort medium [--mode workspace] [--instructions TEXT]
  ├─ spawn `codex app-server --listen unix://<private socket>`
  ├─ initialize + initialized          (the notification is mandatory)
  ├─ thread/start --cwd --model …      (one thread, owned for the worker's life)
  ├─ register in the registry          (ordinary agent row; peers address it normally)
  ├─ poll inbox → turn/start           (one turn per message, FIFO)
  ├─ item/agentMessage/delta → reply back to the sender
  ├─ thread/status/changed → registry status
  └─ idle read between turns           (see "The idle read" — status/quota do NOT arrive by themselves)
```

**Identity.** The worker registers as `agent-<name>` with display name `<name>` — the same shape
`SPANREED_AGENT_NAME` mints for a Claude session, so a restarted worker keeps the id its peers
already hold and can also be addressed by name. Its registry `pid` is the **worker process's own**:
that process's liveness *is* the entry's liveness, which is what `pid` means (`protocol.md`); there
is no Claude session behind it. A restarted worker starts a **fresh thread** and does not resume the
old one.

### Two properties a Claude session does not have

**Status is authoritative.** `thread/status/changed` reports real `active`/`idle` transitions, so a
Codex worker's status is observed rather than self-declared. Claude's is best-effort (see "Agent
status" above), and `last_seen` has been shown unreliable in practice
(`Monkopedia/spanreed#55`). Where the two disagree, the Codex mechanism is the one to copy.

Two qualifications this section originally lacked. First, *observed* only works if somebody reads
the socket — see "The idle read" below. Second, the mapping is not free: Codex reports two states
and the bus has four, and a worker has no human to need, so `active`→`working` and `idle`→`idle` is
the whole map; `needs_input` and `blocked` are unreachable for a Codex worker. A value outside the
map is logged and ignored rather than guessed at. Because the notification's exact payload shape is
not in either vendored schema (they cover requests, not notifications), the worker also sets
`working`/`idle` around each turn itself as a floor — an observed transition overwrites that
whenever one arrives, so the authoritative source still wins where it exists.

**Quota is observable.** `account/rateLimits/updated` arrives unprompted during a turn. A worker
burning the owner's ChatGPT allowance can say so on the bus instead of failing opaquely later. v1
*records and logs* each snapshot (it is in the worker's log, and the latest one is on the worker
object); proactively mailing a peer about quota is not implemented.

### Startup configuration

The worker's model, reasoning effort and persona are fixed when it starts. The parameter names
below are taken from `codex app-server generate-json-schema`, which is authoritative and on disk —
they are not guesses, and the split between the two calls is real:

| Flag | Passed on | Notes |
|---|---|---|
| `--cwd` | `thread/start` | **Required, no default.** See below. |
| `--model` | `thread/start`, re-sent per `turn/start` | Ids come from `models_cache.json`. |
| `--effort` | **`turn/start` only** | Not accepted by `thread/start`. A worker-level effort must therefore be re-applied on every turn — it cannot be set once at thread creation. |
| `--personality` | both | |
| `--service-tier` | both | `serviceTierForTurn` also exists, turn-only. |
| `--instructions` | `thread/start` | Sent as **`developerInstructions`**, appended to a built-in bus preamble. This is where a worker is told it is *on a bus*: that input is mail from another agent, that its reply is sent back as mail, and that a body is data rather than an instruction. Without it the worker behaves like a terminal session that does not know why it is being spoken to. |
| `--mode` | `thread/start` (`sandbox`) + every `turn/start` (`sandboxPolicy`) | `workspace` (default) \| `danger`. See "Modes" below. |
| `--name` | neither | Bus identity only: the worker registers as `agent-<name>`. |

**`sandbox` and `sandboxPolicy` are different parameters, and both are sent.** `thread/start` takes
`sandbox`, whose type is the `SandboxMode` *enum* (`read-only` / `workspace-write` /
`danger-full-access`). `turn/start` takes `sandboxPolicy`, whose type is the `SandboxPolicy`
*object* (`{"type": "workspaceWrite", "writableRoots": [...]}`). Only the object carries
`writableRoots`, which is what actually scopes writes to `--cwd`, so the policy is re-sent on every
turn alongside `effort`. Sending the object under the enum's name is the exact class of mistake
app-server accepts and ignores — checked against `ClientRequest.json`, not inferred.

### Modes

**These mirror Codex's own three permission modes rather than inventing a fourth
vocabulary**, because the thing being configured is Codex's, and a name we made
up would have to be kept true to software we do not control.

| `--mode` | `sandbox` (thread) + `sandboxPolicy` (turn) | `approvalPolicy` | Who answers an approval |
|---|---|---|---|
| `ask` | `workspace-write` / `workspaceWrite` with `writableRoots: [--cwd]` | granular, `sandbox_approval: true` | **the operator, at the worker's terminal** |
| `auto` (default) | same | `on-request`, worker answers | the worker, every decision logged |
| `full` | `danger-full-access` / `dangerFullAccess` | `never` | nobody; warns at startup and every turn |

**Both levels are sent.** `thread/start` takes `sandbox` (a `SandboxMode` enum)
and `turn/start` takes `sandboxPolicy` (an object with `writableRoots`). Sending
only the turn-level one confines nothing — measured on 2026-09-17, see
[findings.md](findings.md). The doctor had that bug and reported it as a total
absence of confinement, which was its own defect and not Codex's.

### Approvals in `ask` mode go to a terminal, never to the bus

The bus is for work. An approval stream on it would drown the messages it
exists to carry, so `ask` prompts on the worker's own stdin and blocks there.

Two decisions follow, both the owner's (2026-09-17):

- **It waits indefinitely.** No timeout, no auto-decline. A queued message is
  not lost and nothing is refused on the operator's behalf; the cost is that
  one unanswered prompt stalls the worker and everything behind it, which the
  worker says loudly on the terminal.
- **It refuses to start without a TTY.** `ask` mode checked against
  `stdin.isatty()` at startup, exiting with the reason. An `ask` worker with
  nobody to ask would block on its first approval forever while looking
  healthy — the silent-failure shape this project has hit repeatedly, and the
  one case where waiting indefinitely turns from a choice into a hang.

### What spanreed does NOT claim

**spanreed does not confine Codex.** It selects Codex's own sandbox and approval
settings, answers the approvals it is asked to answer, and records every one.
Whether a selected sandbox actually holds is a property of `codex-cli`, not of
this project, and it is checked by `spanreed codex --doctor` rather than
asserted here.

This is a correction. An earlier version of this document called `--cwd` "the
worker's entire blast radius" — a claim invented as a design decision and then
defended against software we do not control. Two things it could not survive:
an approval channel that may not fire at all (Codex requested no approval for a
write outside `writableRoots` on 2026-09-17), and a shell command, whose effects
no path check can bound. `--cwd` is now what it always actually was: the
directory the worker works in, the value passed to `writableRoots`, and the
scope of the checks this worker performs itself.

### `--cwd` is required, and what it actually bounds

Auto-approval plus any-sender means **an unauthenticated inbox write becomes code execution**. That
follows from the trust model, which was never a claim that the bus is *authenticated* — only that,
single-user, it need not be. A Codex worker is the first thing on this bus where that distinction
has teeth, because the other end of a message is now a shell rather than a model's judgement.

**What `--cwd` bounds is narrower than the sentence that used to sit here.** It is the value passed
to `writableRoots`, so it is what Codex is *asked* to confine writes to; and it is the scope of the
checks this worker performs on the approvals it is asked to answer. It is not a bound on what a
shell command does once running, and — as of 2026-09-17 — it is not known to be a bound Codex
enforces at all. See "What spanreed does NOT claim" above.

Two rules follow, and they are not negotiable in the way the table above is:

1. **`--cwd` has no default.** Not `$HOME`, not the process's working directory, not whatever
   `config.toml` marks trusted. A worker started without `--cwd` refuses to start. The machine this
   was validated on has `[projects."/Users/monk"] trust_level = "trusted"`, so an inherited default
   would have scoped every worker to the entire home directory.
2. **Approvals are logged, both outcomes,** to `~/.claude/spanreed/codex/<name>.log` (under
   `$SPANREED_STATE_ROOT` when set — it is bus state, so it lives with the rest of it). Every
   `execCommandApproval` and `applyPatchApproval`, the command or path, and whether it was approved
   or declined for being outside `--cwd`. Per rule 7, verbose and legible: the owner wants to *see*
   what a Codex agent did on their behalf, and an auto-approved command that appears nowhere is the
   one that cannot be reviewed. The same file carries startup config, every turn, every non-delta
   notification, and the auth refreshes — **never a credential**. A worker whose log **cannot be
   written** — an unwritable state root, a `codex/` directory it may not create — **refuses to
   start**, with the reason as a sentence on stderr rather than a traceback. A worker that
   auto-approves commands for unauthenticated senders and cannot record what it approved is not a
   degraded worker, it is an unreviewable one.

### When things break

Fault-injected and pinned by `tests/unit/test_codex_worker_faults.py`. The ordering behind every
row: a worker that wedges silently is the worst outcome (that is `Monkopedia/spanreed#55`, which
cost three days), a worker that exits loudly is acceptable, a worker that recovers is best. Every
row produces a log line naming what happened, what the worker did, and what to check.

| Fault | What the worker does |
|---|---|
| app-server dies or closes the socket **mid-turn** | Replies to that sender saying the turn was lost and not retried, logs the drop *and the child's exit status*, then **exits 1**. The queue behind it cannot run without a thread. |
| app-server never binds its socket, or exits after the handshake | `FAILED TO START`, with the server's own captured output and its exit status, then exit 1. Nothing is left in the registry. |
| the socket **file** is unlinked while connected | Nothing. An established unix socket is a descriptor, not a path. |
| a colossal write during a turn | Absorbed by the drain thread, which runs from the moment the child is spawned. This is the 64KB-pipe hang that cost the spike thirty runs; it is now pinned at 1MB *mid-turn* as well as before the bind. |
| a malformed frame, a JSON frame that is not an object, or a `params` member that is not an object | **Skipped and reported** as a `PROTOCOL FAULT` line in the worker's log. WebSocket frames are self-delimiting, so one bad frame does not desynchronise the stream — but a skip nobody is told about is exactly #55's shape. A non-object `params` becomes `{}`, which every approval path already treats as a decline. |
| a response for an id we never sent, or a late one for an id we did | Discarded, and logged as *which of the two it was*: an id outside the range we have issued means something else is on this socket; an id inside it means the server answered a call we had already timed out. |
| `turn/start` returns a JSON-RPC error | The sender gets the error as its reply; the worker stays up. |
| no terminal turn event before the deadline | The sender is told the turn **did not finish**, with any partial text. Accepted is not completed. |
| an unknown server→client request | Answered `-32601`. Never dropped: app-server blocks on these with no timeout. |
| the inbox is unreadable (a truncated line, bytes that are not UTF-8) | Logs the file, the error, and how to repair it, then **exits 1**. Polling an inbox that answers with an exception is the "alive and ingesting nothing" failure. |
| the inbox file is deleted | Treated as empty. Later mail still runs. |
| mail arrives during a turn | Runs as the next turn, in order. Nothing is lost. |
| the sender deregisters before the reply | The reply text goes to the log in full, the cursor still advances, the worker stays up. |
| `--cwd` is a symlink | Resolved once, at construction. The sandbox is scoped to the real directory and both spellings get the same approval verdict. |
| `--cwd` is deleted after start | Every turn is **refused** with a reply naming the reason, and the worker stays on the bus — the directory may come back. No turn runs without its boundary. |
| `--cwd` is not writable, in `workspace`/`danger` | A startup warning naming it. Otherwise the only symptom is the model reporting failed edits as its own fault. |
| `auth.json` missing, unreadable, malformed, or lacking either key | Declines (`-32601`) and says which file and which key. Never invents a token, and never logs one. |

Anything the list above did not predict is caught at the top of the poll loop, logged with its
traceback as an `UNEXPECTED FAILURE`, and the worker exits 1. This ships to a machine its author
cannot debug on, where a traceback on an unwatched terminal is the same as no report at all.

`thread/start`'s result is read in both shapes it has returned across versions (`threadId` and
`thread.id`). The worker previously read only the flat one while `--doctor` read both, so a server
answering the other shape would have produced a doctor that passed and a worker that died on the
same call.

### The idle read

The sketch above says `thread/status/changed → registry status`, and an earlier revision of this
document implied that and `account/rateLimits/updated` simply *fall out* of running turns. They do
not. The client is synchronous: it reads frames off the socket only while it is inside a call, so
between turns — which is most of an idle worker's life — nothing is read and those notifications sit
in the kernel buffer. A worker's registry row would then report whatever the last turn left behind.

So the loop's empty branch is an explicit **idle read**: when the inbox has nothing, the worker
spends its poll interval reading and dispatching whatever the server has sent since the last call.
Status and quota are therefore current between turns, and the read doubles as the poll's pacing —
the worker blocks on the socket rather than on a `sleep`.

### Authentication: `account/chatgptAuthTokens/refresh`

On a `401`, app-server asks the *client* for a token and blocks on the answer. A worker answers from
`$CODEX_HOME/auth.json` (default `~/.codex`) — `tokens.access_token` and `tokens.account_id` — and
replies `{accessToken, chatgptAccountId}`. If the file is unreadable or lacks either field the
worker **declines** (`-32601`) and says so loudly in the log: an invented token fails later and
further from the cause. The token value is never logged, in any form.

### Deliberately not decided yet

- **Cross-host workers.** `conjoin` plus auto-approve means a write on host A executes on host B.
  Not blocked here, but it has not been thought about. (#55's registry sync is fixed — sync is now
  push *and* pull, and its state is on disk — so the remaining question is the security one, not a
  correctness one.)
- **Sender-visible quota.** The worker logs `account/rateLimits/updated` but does not tell anyone on
  the bus. What the threshold would be, and who gets mailed, is undecided.

Resolved since:

- **A turn that fails** (decided 2026-09-16): `turn/failed` and `turn/aborted` send **the error back
  to the sender as an ordinary reply**, threaded with `in_reply_to`, carrying any partial output.
  No retry — a retry would re-run a turn whose side effects already happened — and never silence,
  which would leave a peer blocked on a reply that is not coming. A turn that produces no terminal
  event before the worker's deadline, and one that completes with no agent message, both reply
  saying exactly that.

## Cross-host: the SSH bus-bridge

Single-host spanreed coordinates through a shared local filesystem with no daemon. Cross-host can't share that filesystem safely (`flock` and append-atomicity don't hold over network FS) and the PID-based liveness model is local by definition. Rather than introduce a network broker or a shared mount, we **bridge two independent local buses over a persistent SSH duplex pipe**. SSH gives us authenticated, encrypted transport for free and makes "you can reach the box" the authorization model — which matches the single-user trust assumption exactly.

### Shape

A symmetric **bridge process** runs on each machine, connected by one long-lived SSH connection. One side initiates:

```
hostA:  spanreed conjoin hostB
            └─ ssh hostB <abs-path>/spanreed conjoin --serve
               (the ssh child's stdin/stdout IS the duplex pipe)
```

Both ends run identical bridge logic. `connect` owns the SSH process and the reconnect loop; `serve` speaks the pipe over its own stdin/stdout. This is the `git`-over-SSH / `rsync --server` pattern. The bridge is dedicated infrastructure — it is *not* a Claude session and never wakes one on a timer.

### Registry sync: push *and* pull

The bridge advertises its own host's live agents on a timer, and separately **asks** the peer to advertise (`registry-request`) until a snapshot actually arrives. Two halves, deliberately, because the push half alone cannot detect its own failure.

Issue #55 is the failure it could not detect. In the reported topology the initiator's agents propagated to the peer and the peer's never came back; since `_resolve_recipient` validates against the *local* registry, the initiator could address nobody on the peer. Message transport was unaffected in both directions, so the only symptom was `'<id>@<host>' is not a registered agent_id` — which reads as "that agent doesn't exist".

The design response is not a patch to whichever push went missing; it is that **no side should depend on the other side's timer for state it needs.** Three properties follow, and each closes a way the old design could go quiet:

- **A side that has not been told asks.** The pull re-fires on every sync tick while `last_registry_at` is null, so a snapshot lost to a race, a dropped frame, or a peer that simply never pushed is recovered on the next tick rather than never.
- **Every silent drop became a recorded one.** A registry frame arriving before the handshake used to vanish into an `if peer_host is not None` with no else. A frame this version could not model used to raise out of the reader thread, killing it — after which the bridge kept forwarding mail from its main thread while ingesting nothing, which is precisely "healthy bridge, no sync". Both now write a `note` on the peer record and carry on.
- **An empty answer is distinguishable from no answer.** The `registry` frame carries the counts behind its list (`local_rows`, `stale_local_rows`), so a peer advertising zero agents says so *and* says whether it has rows that failed its own liveness check. "The peer has nothing live" and "the peer never spoke" are different faults on different machines.

**Why not have the initiator pull once at handshake and be done?** Because a one-shot pull has the same blind spot as a one-shot push: it cannot tell a peer that answered with nothing from a peer that did not answer, and it has no second chance if the answer is lost. The cost of re-asking is one line on a pipe that is already sending a keepalive at the same cadence.

### Bridge state is on disk, because the failure was invisible

Each bridge writes `peers/<host>.json` — attached-at, last frame, last registry sync and its size, counts of syncs and requests, detach time, and a free-form note. Schema and semantics in [`protocol.md`](protocol.md#peershostjson--peer-records).

This exists for a stated reason: the reported bug was diagnosable only from state that nothing printed, on a machine the owner could not attach a debugger to. `spanreed list` and the `list_peers` MCP tool now render these records in full, in words, with the remedy named — so the whole diagnosis fits in one pasted terminal buffer. That is the visibility-over-hiding principle applied to the bridge, and it resolves the "`@host` UX in `list_agents`" open question.

Link liveness reuses the agent liveness model exactly (bridge PID alive + start-time match), so a link's state can never disagree with the state of the mirrored entries that bridge owns. Records are **kept** after teardown with `detached_at` set: "a bridge was here and died" is a diagnosis, and deleting the file would make it read as "no bridge was ever configured".

### The core trick: reuse inboxes as the outbound queue

The bridge **mirrors the peer's live agents into the local registry**, qualified by host (`agent-X@hostB`) and owned by the bridge's own PID. Both self-set presence fields cross the bridge: a mirrored entry carries the remote agent's `focus` *and* its `status`, so the "who needs a human" scan over `list_agents` (`status ∈ {needs_input, blocked}`) sees remote agents exactly as it sees local ones. Everything else falls out of the existing primitives with no MCP changes:

- A local agent sends to `agent-X@hostB` → ordinary `send_message` → lands in `inboxes/agent-X@hostB.jsonl` locally.
- The bridge tails every `*@hostB` inbox and forwards new lines over the pipe.
- On hostB, the bridge appends the message to the *real* local `inboxes/agent-X.jsonl`. hostB's agent-X monitor (`tail -F`) fires exactly as for a local message.

Replies are symmetric: agent-X replies to `agent-Y@hostA`, which lands in hostB's `inboxes/agent-Y@hostA.jsonl`, which hostB's bridge tails and forwards back. Purely-local traffic never touches the bridge (it lands in bare inboxes, not `*@peer` ones).

### Identity rewriting

Global identity is `agent-X@homehost`; on its home host the agent is the bare `agent-X`, on a foreign host it's `agent-X@home`. The **sending** bridge rewrites addresses into the *receiver's* namespace before putting a frame on the pipe (`to` = bare local id on the receiver; `from` = qualified with the sender's host), so the receiving bridge just appends. See `protocol.md` for the exact rules.

### Properties that fall out for free

- **No cross-host heartbeat.** Remote-agent liveness is just "present in the peer's latest registry snapshot," which the peer computes with the local PID + start-time check. The snapshot's *age* is recorded on the peer record, so a caller can see how old that evidence is rather than assuming it is current. The bridge's own PID backs the mirrored entries, so if the pipe dies the remote agents correctly vanish from `list_agents`.
- **Store-and-forward across disconnects.** If the pipe is down, outbound messages accumulate durably in the `*@peer` inbox files; on reconnect the bridge resumes from its saved cursor (the existing `cursors/` mechanism) and drains the backlog.

### Launch and prerequisites (empirically settled)

- `spanreed` must be installed on each bridged host.
- The remote `spanreed` is invoked by **absolute path**, because a non-interactive SSH command gets a stripped `$PATH` (`~/.local/bin` is typically added in `.zshrc`, which login/non-interactive shells don't source). The `connect` side discovers the path once via an interactive-shell probe — `ssh host 'zsh -ic "command -v spanreed"'` returns the bare path cleanly — then launches `serve` by that path.
- **Key-based (non-interactive) auth is required**: the bridge re-establishes itself without a human present, so it can't answer a password prompt.

### Reconnect

`spanreed conjoin <host>` is a long-lived **foreground** command. When the pipe drops it re-establishes it with exponential backoff + jitter (a connection that stays up long enough resets the backoff). A dead pipe is detected three ways: EOF on the SSH child, SSH keepalive (`ServerAliveInterval`), and a receive-side watchdog (no frame — not even the peer's pings — within `recv_timeout`). SIGINT/SIGTERM tear down cleanly: clear the mirrored `@peer` entries and kill the SSH child.

Delivery across a drop is **at-least-once**: the outbound cursor advances only after a confirmed send (nothing lost), and delivery dedupes by `msg_id` (nothing double-delivered on resend). **Supervision is deliberately out of scope** — `conjoin` restarts the *pipe*, not itself. If you want it to survive reboots or crashes, wrap it in systemd/launchd/tmux.

### Scope (v1)

Point-to-point: two machines, one direct bridge, launched manually per pair. Transitive/multi-hop routing (B reaching C through A) and auto-discovery of peer hosts are deferred — see `open-questions.md`.
