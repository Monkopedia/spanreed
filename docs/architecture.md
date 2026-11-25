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
- Persistence across machine reboots (except for sleepers; see "Sleepers" below, whose records survive a reboot by design)
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
               --model gpt-5.6-sol --effort medium [--mode auto] [--instructions TEXT]
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
| `--mode` | `thread/start` (`sandbox`) + every `turn/start` (`sandboxPolicy`) | `ask` \| `auto` (default) \| `full`. See "Modes" below. |
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
only the turn-level one confines nothing — measured on 2026-09-17, recorded in
[findings.md](findings.md#incident-the-confinement-measurement-that-motivated-the-three-mode-redesign-was-taken-against-a-thread-with-no-sandbox-2026-09-17).
The doctor had that bug and reported it as a total absence of confinement, which
was its own defect and not Codex's — read the entry before citing the run for
anything else, because it bounds what that measurement can support.

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

Three details settled while implementing, recorded here because two of them are
wire format:

- **The `granular` object is sent complete.** `GranularAskForApproval` requires
  `mcp_elicitations`, `rules` and `sandbox_approval`; all three are sent, plus
  the optional `request_permissions` and `skill_approval` at their schema
  defaults, so what the worker asked for does not depend on a default that is a
  property of a `codex-cli` version. `sandbox_approval`, `mcp_elicitations` and `rules` are
  `true`; the two optional ones are `false`, because neither is a question this
  worker can put usefully and both outlive the turn the operator is looking at.

  **Two things this does NOT mean.** The booleans' *polarity* is not established
  by the schema — `ClientRequest.json` defines all five as bare
  `{"type": "boolean"}` with no descriptions, so "`sandbox_approval: true` makes
  an escape a question" is an inference from the field's name. If `true` in fact
  means *grant this category without asking*, `ask` would be the most permissive
  confined mode rather than the least. See "Not verifiable here".

  And **`mcp_elicitations: true` does not mean elicitations reach the operator.**
  They are declined in every mode, `ask` included: an elicitation wants
  structured content and a y/n cannot supply it, so a decline is the only honest
  answer. The flag asks app-server to route the category to a client, which makes
  the decline visible in the log rather than settled invisibly upstream. An
  earlier version of this paragraph claimed the opposite.
- **A terminal that goes away declines, and says it was not asked.** EOF on
  stdin is not a timeout, and no amount of further waiting produces an operator,
  so the request fails closed. The log line says in full that nobody answered
  it, because an operator reading it later must be able to tell it from an
  answer they gave. The TTY check at startup is what makes this rare.
- **The operator's thinking time is not charged to the turn.** A turn deadline
  measures the *server's* silence; time the worker spends inside its own
  approval handler is this process holding the loop, and in `ask` mode that is a
  human reading a prompt that is allowed to take as long as it takes. It is
  credited back to the deadline, so the worker cannot take an answer and then
  tell the sender the turn never finished. A server that actually goes quiet
  times out exactly as before.
- **`--doctor --mode ask` does not exercise the prompt.** The doctor answers
  approvals from the same policy an `auto` worker uses; everything else it sends
  in that mode — both sandbox levels, the granular `approvalPolicy`, the
  decision enum — is what an `ask` worker sends. The run says so in its own
  output, because a diagnostic read as evidence for a path it never ran is this
  project's most expensive recurring mistake.

### Not verifiable here

Four things about Codex's behaviour that this repository cannot settle, because
`codex` is not installed on the host where it is developed. `spanreed codex
--doctor` exists to answer them on a machine where it is.

1. **Whether the granular booleans mean what their names suggest.** The schema
   types them and does not describe them. If `sandbox_approval: true` grants
   rather than asks, `ask` is the most permissive confined mode. Highest-risk
   unknown here.
2. **Whether app-server honours the thread-level `sandbox`** now that it is sent.
3. **Whether the granular `approvalPolicy` is applied at all**, as opposed to
   accepted and ignored.
4. **Whether `item/permissions/requestApproval`'s decision enum is accepted** —
   it is still inferred by analogy with its siblings, since `ServerRequest.json`
   defines requests and carries no response enum for it.

And one about this project rather than Codex: **the 2026-09-17 measurement that
motivated the three-mode redesign has not been re-taken since the doctor was
fixed.** It was made with no thread-level sandbox, so what it showed is what
that configuration predicts. The position above does not depend on it.

### What spanreed does NOT claim

**spanreed does not confine Codex.** It selects Codex's own sandbox and approval
settings, answers the approvals it is asked to answer, and records every one.
Whether a selected sandbox actually holds is a property of `codex-cli`, not of
this project, and it is checked by `spanreed codex --doctor` rather than
asserted here.

This is a correction. An earlier version of this document described `--cwd` as
bounding everything the worker could touch — a claim invented as a design
decision and then defended against software this project does not control. What
it could not survive was not a measurement but a reading of the schema: a shell
command, whose effects
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
   command and file-change approval (`execCommandApproval` / `item/commandExecution/requestApproval`,
   `applyPatchApproval` / `item/fileChange/requestApproval`), under the method name actually
   received, the command or path, and whether it was approved or declined for being outside
   `--cwd`. Per rule 7, verbose and legible: the owner wants to *see*
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
| another **live** worker already holds this `--name` | **Refuses to start**, naming the pid that holds it. The check runs once before anything is spawned, and again atomically inside the registry lock as part of registering, so there's no window between checking and taking the name. Two workers on one name would both run every message, so in `auto`/`full` every command would run twice, and whichever exited first would unregister the other. A **stale** holder (a worker that crashed without deregistering) does not block a restart. On exit, a worker deregisters only a row that is still its own. |
| `--cwd` is a symlink | Resolved once, at construction. The sandbox is scoped to the real directory and both spellings get the same approval verdict. |
| `--cwd` is deleted after start | Every turn is **refused** with a reply naming the reason, and the worker stays on the bus — the directory may come back. No turn runs without its boundary. |
| `--cwd` is not writable | A startup warning naming it. Otherwise the only symptom is the model reporting failed edits as its own fault. |
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

## Sleepers: agents that sleep and wake on mail

**Status: built, not yet released.** Stage 1: the record, `create`, `list` and
`retire`, and presence from the record at every liveness site. Stage 2: the
transcript read-check, waking on mail, the runner, and the Monitor's replay.
Stage 3: the turn hooks, idle detection, `request_sleep`, and ending Claude.
Measured end to end in findings Tests 12 and 13. Managing sleepers across
`conjoin` (#86) is being built in stages, and nothing reaches a peer yet; see
"Managing a peer's sleepers over `conjoin`".
Adopting a running agent as a sleeper is designed, not built; see "Adopting a running
agent".
The resume mechanics are measured in
[findings.md](findings.md#test-6-can-a-claude-session-in-tmux-be-exited-and-resumed-by-session-id-2026-11-13).
Anything not measured is listed under "Not verified yet" rather than stated as fact.

A **sleeper** is a Claude Code session spanreed manages inside tmux. You create it
with a name and a directory. When it goes idle, or asks to, its Claude process exits
and frees its memory. It stays on the bus, addressable by name, and **the next
message wakes it**: spanreed resumes the same conversation in tmux. It sleeps again
when it goes idle.

This is the case native cross-session messaging cannot cover: a session that isn't
running can't receive a native message. A sleeper's inbox is a file, so mail waits,
and arriving mail is what brings the agent back. (Related: #35, mail sent while a
session is down is never surfaced.)

### Decisions

**Confirmed by the owner** in the spanreed session on 2026-11-13 ("1–5 confirmed, 6
yes"). The confirmation is relayed verbatim on #85 (comment 6863746710) and was
checked against the session transcript in the review of PR #84. It is not yet a
comment from his own account. The background-work rule under "Going to sleep" was
settled the same day, as decision 7 on #85: `request_sleep` stops remaining work,
and the idle timeout never does.

- **Wake resumes the same conversation** (`claude --resume <session-id>`), never a
  fresh session. *The owner's request:* "Resume, if it doesn't resume there is no
  value".
- **The permission mode is chosen at create and kept for the agent's whole life.**
  It is stored in the sleeper record and passed on every launch. *The owner's
  request:* "allow permission mode on create and always keep it".
- **No persistent spanreed process.** This was **the author's proposal**, which
  the owner then accepted ("6 yes"); it was not his own idea. The owner asked "Do we need a persistent process? Or do we
  need locks around how we resume?", and this design is the answer to that
  question. (Earlier in the same conversation the author had read the owner's
  silence as a yes to a per-machine controller process, the opposite answer. That
  is one more reason this needs his explicit confirmation.) Every job a daemon
  would do falls to something already running at the moment it's needed; see "Who
  does what". `docs/open-questions.md` still lists daemon lifecycle in general as
  undecided, and this covers sleepers only.

- **Wait until the agent is up, then deliver.** *The owner's request:* "Can we wait
  for the agent to be up and then deliver the message?" See "Waking".
- **Not at-least-once: "read" is decided by the transcript.** *The owner:* "I don't
  love at least once". The transcript check was the author's proposal, which he
  accepted ("Yeah"). See "What counts as read".

- **Managing sleepers across `conjoin` is wanted, after local sleepers work.**
  This is decision 8, tracked in #86. Its design, and the owner's decisions on
  it (2026-11-25), are under "Managing a peer's sleepers over `conjoin`".

Also the author's, for the owner to see: no ssh agent in a woken sleeper (see
"Environment"), and a signal-first exit.

### The sleeper record

`<state>/sleepers/<name>.json`, owner-only like the rest of the state root. It is the
source of truth, and it survives a reboot:

| field | meaning |
|---|---|
| `name`, `agent_id` | `agent_id` is `agent-<name>`, validated by `validate_agent_id`. An adopted record keeps the id it had (designed, not built; see "Adopting a running agent"), so code must read the field, never derive it from the name |
| `cwd` | the directory Claude runs in |
| `session_id` | a UUID chosen at create; `--session-id` on the first launch, `--resume` after that. An adopted record takes the existing conversation's id, so its first launch is already `--resume` |
| `adopted` | absent unless adopted (designed, not built): when, the pid it took over from, and how `read_through` was chosen; during the handover, the stopped Monitor and the freeze mark, and the phase marker written before the session is signalled |
| `permission_mode` | fixed at create; passed on every launch |
| `model`, `claude_args` | fixed at create; `--model` and the extra `claude` arguments given after `--`, passed on every launch after spanreed's own flags |
| `idle_timeout_s` | default 900 (15 min), set per sleeper |
| `claude_path`, `spanreed_path`, `path_env` | resolved at create and used for every launch; see "Environment" |
| `state` | `asleep`, `awake`, or `sleeping` (committed to sleep, Claude being ended) |
| `generation` | incremented on every wake; part of the tmux session name |
| `tmux_session` | `spanreed-<name>-<generation>` for the current wake |
| `runner_pid`, `runner_pid_start` | written by the waker under the lock, from tmux's pane pid; confirmed by the runner itself |
| `claude_pid`, `claude_pid_start` | written by the runner once it has launched Claude |
| `read_through` | the inbox line number (and its `msg_id`, to detect a rewritten file) up to which every message is **found in the session's transcript**. The no-loss argument rests on this field; see "What counts as read" |
| `sleep_requested` | set by `request_sleep` |
| `last_wake_failure` | why the last wake did not come back, with a timestamp |
| `undelivered_line`, `undelivered_attempts` | the first unread line, and how many launches **where Claude came up** have failed to deliver it; see the poison-line bound |
| `skipped_lines` | every line the poison bound has skipped: line, `msg_id`, when, why. Never overwritten |

### Who does what, without a daemon

**Waking is done by whoever delivers the mail.** Only two paths write an inbox:
`StateStore.send_message` (the CLI, the MCP tools, the Codex worker, and through
them scripts and cron) and `StateStore.append_message` (the `conjoin` bridge). Both
append the message **first**, then call `wake_if_needed(recipient)`. **That is an
invariant for any future inbox writer:** append, then call `wake_if_needed`. A
writer that skips it delivers mail that never wakes anyone. That function:

1. Returns at once if the recipient has no sleeper record. It also does nothing
   when the bridge's `append_message` dedupes a message it already had, since no
   new mail arrived.
2. Takes the sleeper's lock (`<state>/sleepers/<name>.lock`, flock).
3. Decides **under the lock** whether a wake is needed. It is needed when `state ==
   asleep`, **or** when `state` is `awake` or `sleeping` but the runner is dead (pid
   gone, or its start time no longer matches). That covers a crash, an OOM kill, a
   reboot, or the tmux server going away, whether it happens while awake or in the
   middle of going to sleep. Without the `sleeping` case, a runner that died
   mid-exit left a sleeper every later sender skipped, with no `needs_input` (Loss F
   in the review of PR #84).
4. If the runner is dead but `claude_pid` is still alive (an orphaned Claude whose
   runner died), it does **not** resume. Two Claude processes on one conversation
   is worse than a stalled agent. It records `last_wake_failure` and sets the bus
   status to `needs_input`.
5. Otherwise it increments `generation` and **writes the record before launching
   anything**. A waker that dies after `new-session` but before persisting
   `generation` would make the next waker reuse the name and get `duplicate session`.
   Then it launches the runner in a **new detached tmux session named
   `spanreed-<name>-<generation>`**, using `tmux new-session -d -P -F '#{pane_pid}'`
   to get the pane's pid back. It writes `state=awake`, `tmux_session`, `runner_pid`
   and `runner_pid_start`, and releases the lock.

The session name is new on every wake for a reason: the previous runner's session
can still exist for a moment after it has marked itself asleep (the measured "Loss
A" in the review of PR #84). A fixed name made the next wake fail with `duplicate
session`, and that mail went unread. `spanreed sleeper attach X` finds the current
session name for you.

**If the wake fails, the send still succeeds.** For example: no `tmux` on the
sender's PATH, a sandbox that can't reach the tmux socket, or `tmux` exiting
non-zero. The message is already in the inbox. The failure is recorded in
`last_wake_failure`, the bus status goes to `needs_input`, and the mail waits for
the next send or a `spanreed sleeper wake X`. A wake failure must never be silent,
and must never cost the sender its message.

**The runner** (`spanreed sleeper _run <name> <generation>`) runs inside the tmux
pane and exists only while the agent is awake. On start it takes the lock and
**confirms it is the runner the record names**. If `generation` has moved on, or
`runner_pid` is a different live process, it exits without launching anything. If
the waker died before writing the record, the record has **no live runner**
(`asleep`, or `awake`/`sleeping` with a stale `runner_pid`), and the runner claims
it by writing its own pid. So a second
concurrent wake starts a runner that sees the first one and exits.

Once confirmed, it launches Claude as its child (recording `claude_pid`), watches
for idleness and `sleep_requested`, and runs the sleep transition when Claude exits
for any reason.

**A failed wake comes in two forms, and the runner detects both:**

- **Claude can't be launched, or exits by itself without a model turn while
  mail is unread** (`claude` missing, a deleted transcript), **with any exit
  status**. A clean exit counts too: relaunching would only repeat it as fast as
  Claude starts (the review of PR #90 measured 443 launches in 5 s before this
  rule). The same exit with nothing unread is not a failure; the sleeper just
  sleeps. An exit the runner caused itself, by ending an idle Claude, is never a
  failure; that path is bounded separately (below).
- **Claude stays up but the model can't run.** An expired login or a rate limit
  doesn't make Claude exit. It answers every turn with a synthetic error record
  (`authentication_failed`, `rate_limit`). The detector keys on the **`error`
  field**, not on `model == "<synthetic>"`: Claude Code also writes synthetic
  records with no `error` ("No response requested." after a resume, #88), and
  those are not failures. A launch with new error records and no new real turn
  failed.

Either way it records `last_wake_failure` with the reason, sets `needs_input`,
sets **`state=asleep`**, and exits. It does **not** retry in a loop. The next mail,
or `spanreed sleeper wake X`, tries again. Mail is never marked read on a failed
wake, by the rule in "What counts as read".

**A third case, bounded rather than detected** (#88): unread mail that never
starts a turn at all, with no error either (a Monitor that cannot read the inbox,
say). Each launch goes idle, is ended, finds the mail unread, and relaunches, once
per idle timeout. The runner gives up after 3 consecutive launches with no real
turn, records the reason, and sleeps with the mail unread.

### Going to sleep

**Idle detection.** The plugin's hooks for `UserPromptSubmit`, `Stop` and
`Notification` call `spanreed sleeper _activity <event>`, but only in a sleeper
session; elsewhere they do nothing. That writes the last event and its time to
`<state>/sleepers/<name>.activity`. The runner treats the agent as idle when the
last event is `Stop`, more than `idle_timeout_s` has passed, **and no background
work is running**. A pending `permission_prompt` also counts as not idle, and sets
`needs_input`.

**Background work is not idle.** `Stop` fires when a *turn* ends, but a turn can
leave work running: a background shell command, a subagent, a Monitor. Exiting then
would kill that work, which is what Claude Code's exit dialog warns about ("The
following will stop when you exit"). The spanreed inbox Monitor is always running in
a sleeper, so it is the one background task that does **not** block sleep; anything
else does. **The source is the `Stop` hook's `background_tasks`** (measured, findings Test
11). The hook that fires at every turn end lists every running background shell,
Monitor and subagent. The runner records it with the `Stop` activity, and the
idle check reads the last one. Two details:

- **Monitors appear as `type: "shell"`**, so the inbox Monitor is excluded by
  `command == "spanreed inbox-watch"`, not by type. Any other Monitor counts as
  busy.
- **The snapshot is as of the last `Stop`.** Work that ends without starting a
  turn leaves it out of date, so it errs toward busy, never toward sleeping. Most
  background shells do start a turn when they finish (Test 11), which refreshes it.

The candidates this replaces don't work. `TaskCreated`/`TaskCompleted` never fire
for background work. Subagent starts and stops miss work a subagent leaves
running. The exit dialog lists the same thing but means driving the UI.

**The runner never chooses the dialog's "Move to background and exit"** (option 2,
seen in Test 11, not exercised). The design's exit is SIGTERM, which never shows
the dialog. Any fallback path that does see the dialog takes option 1 on the
`request_sleep` path, or Esc on the idle-timeout path.

**Background work and the two ways to sleep** (owner's decision 7 on #85, which
corrects an earlier misreading of his first reply):

- **The agent is responsible for shutting down its own background work.** That is
  the expectation, and the agent's instructions say so.
- **`request_sleep` with work still running stops that work and sleeps.** The
  owner: "if the agent calls sleep now, killing anything else running is fine".
  The exit's "Exit and stop tasks" path, or the signal-first exit, does exactly
  that. The runner logs what was running when it was stopped.
- **The idle timeout alone never stops anything.** Background work other than the
  inbox Monitor blocks a timeout-driven sleep. **Known limitation:** an agent that
  leaves a never-ending job running, such as a dev server or a file watcher, and
  never asks to sleep, stays awake and uses memory until someone stops the job or
  the agent calls `request_sleep`. There is no cap. The owner's words: "we can just
  [list] it as a limitation".

**Asked for.** A new MCP tool, `request_sleep`, sets `sleep_requested`. The runner
acts on it at the next `Stop`. In a session that is not a sleeper, the tool returns
an error rather than doing nothing. `spanreed sleeper sleep X` does the same from a
shell, and `retire X` uses it on an awake sleeper, waiting up to 60 s for it to
sleep before removing it.

**A request does not survive a relaunch.** If mail arrives while the agent is
going to sleep, step 4 relaunches Claude to deliver it, and the request is
cleared then. Otherwise the relaunch, whose launch reads as the last `Stop`,
would be ended `REQUEST_QUIET_S` after it started, before the Monitor had handed
anything over (found in the review of PR #91). The agent can ask again once it
has handled the mail.

**Only the sleeper's own session reports turns.** A Claude the sleeper starts
itself (`claude -p` from its Bash tool) inherits `SPANREED_SLEEPER` and the name,
so its hooks fire too. Hook input carries a `session_id`, and activity from any
session but the record's is ignored; a child's `Stop` would otherwise read the
sleeper as idle mid-turn.

**A plugin older than the CLI reports no turns.** The turn hooks live in the
plugin, which updates separately (`claude plugin update`). Without them the
runner would see no activity and take the launch as the last `Stop`, which could
end Claude mid-turn. So before acting on "idle since launch" the runner checks
the transcript: if real turns have happened since the launch, the hooks are not
reporting, and the sleeper stays awake with `needs_input` and a message saying
to update the plugin.

**Hook cost in ordinary sessions.** The plugin's `UserPromptSubmit`, `Stop` and
`Notification` hooks fire in every session, not just sleepers. Each is a shell
test of `SPANREED_SLEEPER` that does not start Python unless it is set.

### What counts as read: a delivered record in the transcript

Review of PR #84 found three ways the earlier drafts lost mail: during the exit
(Loss B), on the normal wake path (C), and between the idle decision and the
snapshot (D). They had one cause: "read" meant "in the inbox before some snapshot",
which says nothing about whether Claude ever received the message.

**A message counts as read only if a delivered record in the session's transcript
carries its `msg_id`.** Claude Code writes each session's conversation to a
transcript file on disk; the hooks give its path. The transcript is fully written
once Claude has exited, so it is checked only then: at sleep, and before a wake.

**"Delivered" is exact, and does not mean "the id is somewhere in the file".**
Measured in findings Test 7: for a Monitor event, Claude Code first writes a
`queue-operation` / `enqueue` record holding the full event, `msg_id` included, and
only then the `user` record that is the turn's actual input. An exit between the
two would leave the id in the file with the message never received (Loss E). So the check parses the transcript as JSON lines and counts exactly three kinds of
record as delivery:

1. **A Monitor event delivered between turns:** a `user` record whose
   `message.content` is a **string** starting with `<task-notification>`. The
   `msg_id` is read from the event's JSON. A `user` record holding a `tool_result`
   block is not a delivery, even when it contains the same text: an agent that
   prints a transcript produces exactly that (measured in the review of PR #84).
2. **A Monitor event delivered mid-turn:** an `attachment` record with `type ==
   "queued_command"`, with the `msg_id` read from its `prompt`. Other attachment
   types (`file`, `edited_text_file`, …) can carry msg ids from files and edits,
   and are not deliveries.
3. **A `recv_messages` result Claude actually received:** a `tool_result` paired by
   `tool_use_id` with a `tool_use` of the spanreed `recv_messages` tool. The ids
   are parsed **only from the paired `tool_result` block's `content`**, the text
   Claude was given. The record's `message.content` is a list holding that block.
   The block's `content` is a string, and all 2,539 real results measured had that
   form. If it is ever a list of `text` blocks (the API allows it), they are
   joined. Ids count only when that text is **complete `{"result":[…]}` JSON**. Reading
   mail through the tool is reading it, so a message read this way after its
   Monitor event was dropped is not replayed.

   **Never `toolUseResult` or `mcpMeta`.** Those fields hold a clean copy of the
   full result, and Claude never sees them. A recv result can be over the token
   limit, in which case Claude got only `Error: result (N characters) exceeds
   maximum allowed tokens…`. Or it can be persisted, in which case Claude got a
   2 KB preview. Either way the complete id list is still in `mcpMeta`. The review
   of PR #84 measured 561 of 2,539 real recv results like that (544 over the
   limit, 10 previews, 7 errors). Counting their ids as read would mark mail read
   that Claude never received. **A result that isn't complete JSON in
   `message.content` counts as nothing.**

**A candidate record counts only if the model actually ran on it.** That means a
**non-synthetic assistant record** follows it with the candidate in its
`parentUuid` chain: `model` is not `"<synthetic>"`, and there is no `error`.

When the API call fails (rate limit, expired login, server error), Claude Code
still writes the delivered record. It then writes a synthetic assistant record
carrying the error, and the model never runs. The review of PR #84 measured 148
real task-notification records answered only that way: 82 `rate_limit`, 36
`authentication_failed`, 25 `server_error`, 5 other. Some fell off the
conversation chain at a later compaction or resume. 19 were never seen by the
model at all, including 18 bus messages during one weekly-limit window. Counting
those as read is mail loss. A record answered only by a synthetic error counts as
**unread**, and so does a complete `recv_messages` result followed only by one.

Nothing else counts: never `queue-operation`, never a hidden structured copy of
a tool result, and never a substring anywhere in a record. An id the agent wrote itself (an `in_reply_to`, a quoted id in a body, the
agent grepping its own inbox) therefore doesn't count. The implementation's tests
include a negative control for each excluded kind: enqueue-only, a `tool_result`
that isn't from `recv_messages`, a non-`queued_command` attachment, an over-limit
`recv_messages` result whose `mcpMeta` holds the ids, and a task-notification
followed only by a synthetic `rate_limit` record. All must count as unread.

**The one bounded duplicate, stated rather than hidden.** A message Claude saw
only partly is replayed once. That covers only a 2 KB preview, or a full list the
agent read through a separate tool call (a `cat` of the saved output file, say,
which is a `tool_result` this rule ignores). That is the safe direction: the
alternative is guessing it was read. It is one of two cases where "nothing is
redelivered" doesn't hold.

**The second: a compaction can cut a delivered record out of the chain** (#88).
After a compaction, the records before it are no longer ancestors of later real
turns, so a message delivered just before a compaction and answered only after it
can lose its real-turn evidence and be replayed once. The review of PR #84 found
25 real ids in that position across the fleet's transcripts. Again the safe
direction.

**This depends on `msg_id` being the first field of every inbox line.** Monitor
events are truncated at about 514 characters (measured in the review of PR #84:
9,448 of 10,178), and the id survives only because it comes first. That is now an
invariant of the inbox format, and the check proves only that Claude saw a ~500
character preview of the message, not its whole body. The full body is always one
`recv_messages` call away, and the preview says so.

**The transcript is not a promised API, so the check fails safe.** The hooks are
documented to hand over `transcript_path`, but the record format inside is
Claude Code's internal format and could change in any release. Whenever the
transcript is **missing, unreadable, or holds no `user` or `assistant` record of
the known shape** (as if the format had changed under us), every line after
`read_through` counts as **unread**. (The test is the record shape, not "none of
the three delivered kinds": a sleeper that has never received mail has none of
those, and its transcript is fine. A transcript that is missing *before the first
launch* is expected, not a fault.) The cost is a replay, so mail may be seen twice. The runner also sets
`needs_input` with the reason, so the owner learns the check stopped working
rather than finding out through duplicates. **In this fail-safe mode, step 4 does
not relaunch and the poison bound does not count.** The agent sleeps with
`needs_input`, and the backlog is replayed once per wake. Without this, every
check would find everything unread and relaunch. Each relaunch would replay the
whole backlog, so duplicates would grow roughly quadratically, and every line
would end up in `skipped_lines` despite having been delivered. A format change can therefore cost
duplicates, never lost mail. (Prompted by reports, on 2026-11-13, of claude.ai
changing what its conversation exports and containers keep. That is a different
product from Claude Code's local transcripts, which were still being written on
kaladin the same day: 100 since 2026-11-11, including both test sessions. But it
shows the format sits outside our control.)

`first_unread_line()` walks the inbox from `read_through` and returns the first
line whose `msg_id` has no delivered record. It builds the set of delivered ids
**once per check**, in one pass over the transcript. A transcript grows without
bound, so this is a full read each time, at sleep and at wake, never per line.

**A line that never reaches a delivered record** (a corrupt line, or one the Monitor
cannot deliver) would otherwise stop `first_unread_line()` forever: every idle cycle
would relaunch and every wake would replay it. After **3** launches that fail to
deliver the same line, the runner skips it and sets `needs_input`. The bound is
defined carefully, because a careless version loses mail:

- **Only launches where the model actually ran count:** a **non-synthetic
  assistant record** was written during the launch. Neither hook activity nor a
  growing transcript is evidence. A session with an expired login or a rate limit
  stays up, fires `UserPromptSubmit`, and writes a full set of records for every
  Monitor event, all answered by a synthetic error (measured in the review of PR
  #84). A launch whose new assistant records are all synthetic `rate_limit`,
  `authentication_failed` or `server_error` errors is a **failed wake**, not a
  failed delivery, and doesn't count. Otherwise a rate-limit window would skip
  backlog one message at a time, and those messages would never be delivered.
- **Both kinds count:** a wake from `asleep`, and a step-4 relaunch. Counting only
  wakes would never trip on the idle → step 4 → relaunch loop.
- **The counter resets** whenever `first_unread_line()` moves past
  `undelivered_line`.
- **Skipped lines are kept durably** in `skipped_lines` (line, `msg_id`, when,
  why). They don't go in `last_wake_failure`, which each new failure overwrites.
  `sleeper list` shows them. A message is never skipped silently, and a skip is
  never forgotten.

**`read_through` starts at the end of the inbox** when the sleeper is created. A
`retire X` followed by `create X` (`retire` leaves the inbox in place) must not
replay X's whole history into a fresh conversation. An **adopted** record is the
exception: it starts after the last line its conversation's transcript shows as
delivered, since that conversation is not fresh (designed, not built; see "Where
`read_through` starts" under "Adopting a running agent").

The owner rejected an at-least-once design that could redeliver messages; this is
the exact alternative.

**Considered and declined: an explicit acknowledgement.** Every loss path found in
review of PR #84 came from inferring "read" from the transcript. Each one is
patched above, but the transcript is not a promised API. The alternative is for
the agent to call an ack tool for each message it has handled, with only acked mail
counting as read. That would make the transcript irrelevant, and with it API
errors, truncated results and format changes. Its cost is a duplicate whenever the
agent forgets to ack. The owner declined it for now ("Stick with B for now, I don't
want the overhead"; #85). If the transcript format changes under us, this is the
design to switch to, and the fail-safe above keeps that switch from being urgent.

### Waking: wait until the agent is up, then deliver

The owner's model: don't hand a message to a session that isn't ready for it. Wait
until the agent is up, then deliver through the normal path.

- **While asleep, mail just sits in the inbox.** There is no separate queue. The
  inbox is already a file, and nothing reads it while the agent sleeps.
- **On wake,** the runner computes `first_unread_line()` against the transcript,
  which is complete because Claude isn't running. It records the result as
  `read_through` and launches Claude.
- **The sleeper's inbox Monitor starts from that line**, instead of from the end
  of the file (`tail -n 0`). It is a spanreed reader, not bare `tail`. While
  replaying, it **skips any line that already has a delivery** in the sense of
  "What counts as read" (for example, a message read through `recv_messages` after
  its Monitor event was dropped), then follows the file as usual. Without that filter, a replay would redeliver messages
  Claude already had: the at-least-once behaviour the owner declined. Every unread message, including
  the one that woke the agent, reaches Claude **the same way mail reaches an awake
  agent**, and the Monitor starts the turns itself. No first prompt and no special
  path. Non-sleeper sessions keep `tail -n 0`.
- **A crash or reboot while awake** needs nothing extra. The next wake computes
  `first_unread_line()` from the transcript, which is just as complete after a
  crash.

**Measured in findings Test 9:** a reader started mid-file does deliver mail that
arrived while the agent was down. Today's `tail -n 0` never does. But the Monitor
**batches** lines that arrive close together into one event and one turn. A batch
is **cut off at about 3,000 characters**, and lines past the cut never reach Claude.
So the replaying reader hands lines over **one at a time, with a pause between
them** long enough that they arrive as separate events, rather than replaying
the backlog in one burst. The read check doesn't depend on that pacing: a line cut
from a batch has no delivered record, so it counts as unread and is delivered at
the next relaunch or wake. Pacing only avoids that delay. (The same ~3 KB cut
affects awake agents today; that is a separate bug, tracked on its own.)

### Committing to sleep, in this order

1. **Decide.** The last event is a `Stop`, and one of:
   - **the idle timeout has passed and no background work is running** (other than
     the inbox Monitor), or
   - **the agent called `request_sleep`** and a short quiet period has passed with
     no new turn starting. Background work does not block this path; it is stopped
     at the exit (decision 7).

   The quiet period gives a Monitor event queued behind the final turn time to
   start its own turn.
2. Under the lock, set `state=sleeping`. Senders who see `sleeping` append and do
   nothing else; the runner checks the inbox after the exit.
3. **End Claude** (see "The exit"), and wait until the process is actually gone.
4. **Under the lock, compute `first_unread_line()`** against the now-complete
   transcript. If any line is unread, whether it arrived before the decision, during
   the exit, or a moment ago, **do not sleep**. Set **`state=awake`** and relaunch
   Claude in the same runner and tmux session, with its Monitor starting from that
   line. `generation` and `tmux_session` don't change.
5. Otherwise set `state=asleep` and `read_through`, release the lock, and exit.

### The exit

Typing `/exit` into the pane is fragile. It depends on Claude Code's UI: the prompt
must be empty, and the confirmation dialog must keep its current wording, its option
order and its default ("Exit and stop tasks", measured in Test 6). Any of those can
change in a Claude Code release. The runner has a sturdier option: Claude is its
child, so it can end it with a **signal**, with no UI involved.

Plan:
- **Preferred: SIGTERM to Claude.** Measured in findings Test 8. Claude exits
  within a second, its transcript is complete, and a resume restores the
  conversation. No dialog is involved. As with `/exit`, it stops background work,
  so on the idle-timeout path the "no background work" check must already have
  passed.
- **Escalation is SIGKILL to Claude after 15 s, then to every descendant still
  running.** Not a process-group kill: the inbox Monitor's `tail` runs in a session
  and process group of its own, so a group kill misses it (#88), and SIGKILL to
  the pid alone leaves it orphaned on the inbox (Test 8). The runner lists
  Claude's descendants (by parent pid, from `ps`) before signalling, and after
  Claude has exited it kills any of them still alive whose start time is
  unchanged.
- **No `/exit` fallback.** SIGTERM has been clean in every measurement, and the
  dialog would bring back the UI dependence the signal avoids.
- **Either way,** the runner never trusts the signal it raised. It waits for the
  child process to exit before checking the inbox.

A signal skips the dialog, and the dialog is also the only complete list of
background work. The hooks answer "is anything else running?" instead
(`Stop.background_tasks`, measured in Tests 11 and 13).

### Why no message is lost

Take a message M, appended to the inbox by its sender before the sender takes the
lock.

- **M is in the transcript.** Claude received it. Nothing is owed.
- **M is not in the transcript, and its sender took the lock before step 4.** The
  sender saw `awake` or `sleeping` and did nothing more. At step 4, under the lock
  and against a complete transcript, M is unread, so the runner relaunches and the
  Monitor delivers M.
- **M's sender took the lock after step 5.** It saw `asleep` and launched a wake.
  The wake computes `first_unread_line()`, which is at or before M, and the Monitor
  starts there.

So M always ends up in front of Claude. One case isn't immediate: if M's sender
saw `sleeping` with a live runner, and the runner then dies before step 4, M waits
in the inbox until **the next send** (or a `spanreed sleeper wake X`), because no
daemon notices the death. M isn't stranded, since any later sender sees a dead
runner and wakes the agent. But that is the latency the no-daemon decision pays. Because the check reads the transcript,
nothing is redelivered that Claude already received. The same lock, plus the
runner confirming it is the runner the record names, rules out a double wake. The
argument holds **only if the transcript records every Monitor-delivered `msg_id`**,
which is why that is measured first.

### Liveness: where "the record decides" has to change

For an agent with a sleeper record, presence comes from the record, not the pid.
`is_stale(agent)` is a pure function of a registry row today. It becomes
`is_present(agent, store)`, which also checks the sleeper record. Every current
caller has to move to it:

| site | what it does today | what changes for a sleeper |
|---|---|---|
| `store.py` `list_agents` | hides stale rows | asleep sleepers are listed |
| `store.py` `prune_stale` | **permanently deletes** stale rows | must never delete a sleeper's row |
| `store.py` `_resolve_recipient` (live-by-name) | name resolution among live agents | an asleep sleeper resolves by name |
| `store.py` `delivery_verdict` | "session running, monitor tailing" | a new verdict: "queued; agent asleep, wake launched", or the wake failure |
| `store.py` `register_agent(refuse_live_holder=True)` | refuses a live holder (#80) | an asleep sleeper holds its name |
| `bridge.py` `_send_registry` | advertises only live rows | advertises asleep sleepers, so peers can mail them |
| `cli.py` `list` LIVE/STALE | | shows `asleep`, `awake`, `sleeping`, or `awake (runner dead)` / `sleeping (runner dead)`, so a crashed sleeper never reads as healthy |
| `codex_worker.py` name-held check | | a sleeper's name counts as held |

### Environment

A woken runner's environment is the tmux server's global environment. If no server
is running, for example after a reboot, that comes from whichever process happens
to start the server: possibly a cron job with a minimal PATH, an ssh-launched
`conjoin --serve`, or a Claude session whose `CLAUDECODE`/`CLAUDE_*` variables would
leak into the woken session. So **nothing about the launch depends on the tmux
server's environment.** The runner starts from an **empty environment plus an
allowlist**, not from what it inherited minus a few names. A denylist misses
whatever variable leaks next. The allowlist:
- **From the record:** `PATH` and the absolute paths to `claude` and `spanreed`, as
  resolved by `create`.
- **From the account, not the caller:** `HOME`, `USER`, `LOGNAME`, `SHELL`, read
  from the password database.
- **From the tmux pane:** `TERM` (tmux sets it for the pane), `TMPDIR`, `LANG` and
  `LC_*`. None of these can carry a credential or another session's identity.
- **Set by spanreed:** `SPANREED_AGENT_NAME`, `SPANREED_SLEEPER=1`, and
  `SPANREED_STATE_ROOT` (the waker's state root, also passed to the runner with
  `tmux new-session -e`), so the woken session is on the same bus as the mail that
  woke it.

Nothing else comes through, in particular any `CLAUDECODE`/`CLAUDE_*` variable or
a sender's API credentials. **A known gap:** `SSH_AUTH_SOCK` is per-login, so a woken
sleeper has no ssh agent, and `git push` over ssh from inside one fails. That is
deliberate for now, and stated rather than hidden.

### Across `conjoin`

Mail to a sleeper on another machine needs nothing new in the bridge, apart from
advertising asleep agents:
1. The sender's machine queues it in the outbound inbox `agent-X@hostB.jsonl`, as for
   any remote agent.
2. The bridge forwards it, and the bridge on hostB writes it to `agent-X`'s inbox
   with `append_message`.
3. That write runs `wake_if_needed`, **on hostB**, where the tmux session, the login
   and the sleeper record all live.
4. Mail queued in step 1 survives a drop, and mail sent *while* the link is down
   queues too, as long as the `conjoin` for hostB is still running and retrying:
   the reconnect delivers it and wakes the sleeper. Once no `conjoin` for hostB is
   running (stopped, crashed or killed), a send to `agent-X@hostB` is refused
   rather than queued, because nothing would forward it ([#34](https://github.com/Monkopedia/spanreed/issues/34)).

What changes:
- **Peers must see asleep sleepers**, or no remote agent can address them.
  `_send_registry` advertises them (see the liveness table), with the `asleep`/`awake`
  marker. Peers mirror it, and an older peer ignores the field.
- **The serve end of a bridge is ssh-launched**, so a wake it triggers starts tmux
  from an ssh session. That is the `KillUserProcesses` risk under "Not verified yet",
  and the environment allowlist exists for the same reason.
- **A remote sender can't tell "asleep" from "awake" in its delivery verdict** beyond
  what the last registry sync showed. It gets "queued for the bridge", which is true.

**Managing a sleeper on another machine** (create, wake, sleep, retire and list it
from here) is the owner's decision 8 on #85: "Get everything working first, then
let's worry about it, but I would like support for management over conjoin". It is
**designed, and being built in stages**; see the next section. Mail already wakes a
remote sleeper without it.

### Managing a peer's sleepers over `conjoin` (#86)

**Status: partly built; nothing reaches a peer yet.** Stage 1 built the target's
side, tested without a second machine: `spanreed sleeper remote-access`, the
operations shared with the local commands (`sleeper_ops.py`), and the handling of a
`sleeper-op` frame on the target, with its validation, expiry, idempotency record
and audit log (`sleeper_remote.py`). The bridge does not carry the frames yet, so no
request can arrive: that, `features` in `hello`, and the requester's spool and
`--host` commands are later stages. Facts below that come from reading the code say
so; everything that needs a measurement is under "Not verified yet (remote
management)". The question of default-on, and the calls stage 1 had to make where
this section is silent, are in
[open-questions.md](open-questions.md#remote-sleeper-management-86-being-built).
R9 and R10, which this design raised, are decided (decision 8).

#### Decisions

The owner gave these in the spanreed session on 2026-11-25. They are recorded in
comments on #86 of that date, all posted by an agent's account, not by the
owner's own. Only the words attributed to *the owner* are his; the rest is the
design that his words were an answer to, and says so.

1. **The carrier is new frame kinds over the `conjoin` bridge**, in both directions.
   Not a separate ssh command per operation. *The owner:* "conjoin isn't one way, its
   two way … there may not be a bidirectional ssh pipe, this must be a frame over
   conjoin". Either end of a bridge can manage the other end's sleepers, including
   an end with no ssh path back (a Mac behind NAT that dials out).
2. **Off by default, and turned on at the machine whose sleepers are managed.** Asked
   by the author whether opt-in on the managed machine was the right way to authorize
   this, *the owner:* "that sounds fine for now, maybe after a few releases we turn it
   on by default". Whether it becomes default-on is deferred (open-questions.md).
3. **The grant is one per-machine switch, not a per-peer list.** *The owner:* "wait
   are we granting access based on peer name or just opting in? I thought it was more
   like allow connections opt in, not a per-name list". Offered a single on/off
   switch, under which any machine this one is conjoined with may manage its
   sleepers, he answered "yes". Nothing is keyed on a peer's host label.
4. **A remote `create` is not capped beyond a local one.** This and decision 5 are
   not words of his own. The author proposed this summary of the design: "one
   per-machine switch, `spanreed sleeper remote-access on|off|show`, off by default.
   Turning it on requires a person at a terminal. No cap on what remote commands can
   do, with the docs stating what turning it on means." *The owner* answered "yes"
   (14:47:09Z). The summary is quoted, and the attribution corrected, in the #86
   comment of 2026-11-25 14:59. See "What turning it on grants" for what no cap
   means.
5. **Turning the switch on takes a person at a terminal.** From the same summary and
   "yes" as decision 4. Decision 7 says what that check can and cannot do.
6. **His answers to the questions the first draft left open** (R-numbers as in that
   draft): R1, record `PATH` when the switch is turned on: "sure, record is fine". R2,
   one gate for every operation: "one gate for everything". R3 and R4 (a per-peer or
   per-operation grant, and trusting the peer's label) are superseded by decision 3.
   R5, the retention periods: "yes". R6, one operation at a time: "does this queue?
   if so, that's fine"; it does, see "On the target". R7, `attach --host` prints the
   ssh command: "sounds fine". R8, an interrupted operation is reported, never run
   again: "no rerun, correct".
7. **The terminal check guards against accidents, not against agents.** Any process
   running as the user can write the `sleeper_remote_access` key in `config.json`
   directly and never run `remote-access on`, so no check on the command can make
   "a person turns it on" hold against an agent. Asked whether to accept the check
   as protection against accidents only, *the owner:* "I mean this is true of all
   things, claude can rewrite its own settings in the right environment, this is how
   the agentic world works and there's no inherent way around it without crazy
   security controls" (14:58:48Z, recorded on #86). Nothing stronger is added, such
   as a root-owned file. See "What the TTY check does not stop".
8. **R9 and R10, as recommended.** *The owner* answered "recs fine" to the author's
   recommendations (recorded on #86, 2026-11-25 14:54): `--timeout` is capped at
   600 s, the clock allowance is a fixed 30 s, expiry is checked again when a queued
   request starts, and the TTY check is accepted for v1 with its limits stated
   (R10). Decision 7, given after, restates those limits to cover the file as well
   as the command.
9. **The grant is about what ssh already gives, but both ways, and that is
   expected.** On the switch letting any conjoined peer run commands as him, *the
   owner:* "again, this is relatively equivalent to the ssh access, other than the
   fact that it makes it bidirectional, which we should note, this is expected". The
   end that was ssh'd into can also manage the end that dialled it, which it may have
   no ssh access to. These are his words in the spanreed session, recorded on #119 by
   the coder (comment of 2026-11-25 15:39), not a GitHub comment of his own.
10. **Trust is transitive across the conjoin graph, and the model stays as is.**
    *The owner:* "this is an interesting note, we should document this clearly, but I
    don't think we should change the model. it should be clear that when you conjoin
    with someone, you trust their entire bus/network" (his words in the spanreed
    session, recorded in the same #119 comment). See "What
    turning it on grants".

**The target resolves everything.** The target's own spanreed runs the operation, so
paths, `claude` and `PATH` resolve there, and it logs every remote operation. This
was the author's proposal in the first of those sessions, and the owner did not
object. It is not his decision. The first draft of this section listed it as one,
together with the claim that a remote `create` "can do no more than a local one".
That claim is true but misleading, since a local `create` can do anything its user
can, and decision 4 replaces it.

#### The commands

The requester adds `--host PEER` to the local command. `PEER` is the peer's **host
label**, the `@host` suffix its agents carry here (what `spanreed list` shows under
peers), not necessarily the name `conjoin` was given for ssh.

```
spanreed sleeper create --host PEER --name X --cwd DIR --permission-mode MODE [--model M] [--idle-timeout 15m] [--trust] [--timeout S] [-- CLAUDE_ARGS...]
spanreed sleeper list   --host PEER [--json] [--timeout S]
spanreed sleeper wake   --host PEER X [--timeout S]
spanreed sleeper sleep  --host PEER X [--timeout S]
spanreed sleeper retire --host PEER X [--timeout S]
```

- Every argument is sent **as typed** and interpreted on the target. The requester
  does not expand `~`, resolve `--cwd`, parse `--idle-timeout`, or check
  `CLAUDE_ARGS` against the allowlist: each of those means something different on
  the other machine. **The target validates every argument itself** (see
  "Validation on receipt"); nothing the requester checks is relied on.
- **`--cwd` must be absolute or start with `~`** in a remote create, and `~` is the
  target user's home. A relative path has nothing to be relative to on the target:
  the bridge's own working directory is wherever `conjoin` happened to start. The
  target refuses a relative `cwd`. The requester also refuses one before sending,
  but only so the person hears it sooner; a modified requester can skip that.
- `--timeout S` is at most 600 s (R9, decision 8). It is also the request's
  lifetime: see "Every request expires".
- The output is the target's output, printed verbatim, with every line prefixed
  `[PEER] ` so it can't be mistaken for a local result (rule 7: visible, not clean).
  `list --json` prints the target's JSON unprefixed.
- `--host` naming this host is refused ("that is this machine; drop `--host`").
- **Exit codes:** `0` the operation ran and succeeded; `1` it ran on the target and
  failed, exactly as the same command would locally (a held name, a refused
  `claude` flag, no such sleeper), or was `interrupted`; `2` nothing was run
  (no bridge to `PEER`, a bridge or peer too old, a bad argument caught here or
  answered `invalid` or `expired` there, a timeout before sending); `3` it was sent
  and no answer came in time, so **the outcome is unknown**; `4` the target refused
  it because remote management is turned off there.

**`attach` cannot work over a bridge.** It is an interactive tmux client, and the
bridge carries frames, not a terminal. Per R7 (decision 6), `sleeper attach --host
PEER X` asks the target for the sleeper's record (a `list`) and **prints the command
to run instead**, exiting 2:

```
[adolin] sleeper X is awake in tmux session spanreed-X-7. Attach from a shell on adolin:
  ssh -t adolin tmux attach -t spanreed-X-7
(that needs an ssh path to adolin; if there is none, attach at adolin itself)
```

It does not run ssh itself; that would be the per-command ssh carrier decision 1
ruled out, and it fails on exactly the hosts decision 1 exists for.

#### How a CLI call reaches the bridge: a request spool

The CLI is a short-lived process; the bridge is a long-lived one that owns the
pipe, and only its main thread may write to the pipe (`bridge.py`, `_send`). So the
CLI never touches the pipe. It drops a request file where the bridge looks, and
waits for a reply file. Paths are under the state root, owner-only like the rest:

```
sleeper-ops/out/<peer>/<request_id>.json        a request waiting to be sent
sleeper-ops/out/<peer>/<request_id>.sent        sent (or being sent); resent on reconnect until it expires
sleeper-ops/out/<peer>/<request_id>.abandoned   given up on, by the CLI or at expiry; never sent again
sleeper-ops/out/<peer>/<request_id>.reply.json  the target's answer
```

The file's **name is its state**, and every state change is a `rename`, which is
atomic. That is what makes the race between the CLI giving up and the bridge
sending resolvable after the fact:

1. **Before writing anything**, the CLI reads `peers/<peer>.json`. It refuses, with
   exit 2 and the remedy named, when there is no record ("no bridge to adolin has
   ever attached here: run `spanreed conjoin adolin`"), when the link is not
   **attached** (see "Which link state counts as down" below), when this
   host's bridge process doesn't list `sleeper-ops` in `bridge_features` (it was
   started by an older spanreed: restart `conjoin`), or when the peer didn't
   advertise `sleeper-ops` in its `hello` (see "Version skew"). No request is ever
   written for a bridge that cannot carry it.
2. The CLI mints `request_id`, 32 lowercase hex characters (`uuid4().hex`), stamps
   `created_at` and `expires_at` (`created_at` plus the timeout, UTC), and writes
   `<request_id>.json` (temp file, then rename).
3. The bridge's main thread scans `out/<peer>/` on every loop turn, as it does the
   outbound inboxes. For each `.json` whose `expires_at` has not passed, it
   **renames it to `.sent` first**, then sends the `sleeper-op` frame. Only one
   process wins a rename, so two bridges to the same peer can't both send it.
4. The bridge's reader thread receives `sleeper-op-result`, checks that
   `<request_id>.sent` exists, and writes `<request_id>.reply.json`. A result for
   an id it has no `.sent` file for is noted on the peer record and dropped.
5. The CLI polls for the reply. On one, it prints it, deletes both files, and exits
   with the mapped code.
6. **At `expires_at`**, the CLI renames its own file to `.abandoned`. If `.json` →
   `.abandoned` succeeds, the bridge never claimed it: **nothing was sent**, exit
   2. If only `.sent` → `.abandoned` succeeds, it was sent: the CLI looks for a
   reply once more (it may have landed in between), and otherwise reports **sent,
   outcome unknown**, exit 3, naming `spanreed sleeper list --host PEER` and the
   target's audit log as the way to find out. If neither rename succeeds, the
   bridge has already expired the file (below), and the CLI reports outcome
   unknown, exit 3, since it can no longer tell whether it was sent.

**Timeouts:** 30 s by default; 90 s for `retire`, which on the target may wait up
to `RETIRE_WAIT_S` (60 s) for an awake sleeper to finish its turn. `--timeout S`
overrides it, up to 600 s (R9, decision 8). Time spent queued behind another
operation on the target counts against it (see "On the target").

**Management never queues across a down bridge.** Unlike mail, a request that
can't be sent now fails now (step 1) or at its timeout (step 6). A `create` that
fires hours later, when someone reconnects, would surprise whoever had given up on
it.

**Which link state counts as down.** A link is attached, reconnecting or stopped
([`protocol.md`](protocol.md#peershostjson--peer-records), #34), judged by the same
test `spanreed list` uses: `stopping_at` first, then the pid, then `detached_at`.
For a remote sleeper request only **attached** counts as up. **Reconnecting** and
**stopped** are both down: step 1 refuses at once, exit 2, and the error names the
state ("the bridge to adolin is reconnecting (link dropped at …)" or "… is
stopped"). Mail queues while `conjoin` reconnects; a sleeper request does not, since
a reconnect may take hours. A request already written when an attached link drops
is the one exception, and it is bounded: its `.json` or `.sent` file is sent or
resent on reconnect only until its `expires_at`, at most 600 s after it was made.

#### Every request expires

Step 6 runs only while the CLI is alive. A CLI killed by Ctrl-C, `SIGKILL` or a
closed terminal leaves its `.json` or `.sent` file behind, and without a limit the
bridge would send the one, or resend the other on every reconnect, hours or days
later. Cleanup alone doesn't prevent that, and once the target has pruned its
idempotency record (R5) a resend would run the operation a second time. So the
expiry is enforced by the bridge on the requester and checked again by the target,
neither of them relying on the CLI:

- **The requester's bridge never sends or resends a request past its
  `expires_at`.** It checks before each send and each reconnect resend. Once a
  `.json` or `.sent` file is more than 60 s past its `expires_at` (the grace lets a
  living CLI do step 6 itself, so its exit code stays exact), the bridge renames it
  to `.abandoned`. A killed CLI's request therefore lives no longer than its
  timeout, and at most 600 s.
- **The frame carries `expires_at`**, and the target checks it against its own
  clock (R9, decision 8; see "Idempotency" and "Validation on receipt"). A fixed
  30 s allowance for skew between the two hosts applies only to how far ahead
  `expires_at` may be; a deadline that has passed by the target's clock is passed,
  with no allowance. A request that reaches the target expired, with no record of
  it there, is answered `expired` and not run.
  This is the backstop for a requester that doesn't enforce its own limit, and
  what makes R5's pruning safe: a record is pruned after 7 days, a request lives at
  most 600 s, so no request that the target accepts can outlive the record that
  would recognise it as a resend.

**Cleanup:** `.abandoned` files and orphaned replies are removed by the bridge when
they are older than a day. The target's idempotency records are kept for seven days
and pruned by its bridge at start. Both periods are R5's, accepted by the owner
(decision 6).

#### On the target: who runs the operation

The target's bridge receives `sleeper-op` on its **reader thread**, which must not
run it. A `retire` can block for 60 s, and a blocked reader stops reading pings,
so this end's own watchdog (`recv_timeout`, 15 s by default) would tear the link
down under its own operation. Instead:

1. The reader validates the frame (below), checks the switch, checks expiry,
   writes the **idempotency record**, logs `received`, and queues the request for
   a **worker thread** owned by this bridge process.
2. The worker runs operations **one at a time, in arrival order** (R6, decision 6).
   A second operation that arrives while one is running **waits** in the queue
   behind it. When the worker takes a request off the queue, it checks
   `expires_at` again: one whose deadline passed while it waited is answered
   `expired` and not run, because its requester has already reported "outcome
   unknown" and stopped waiting (R9, decision 8). The worker calls the same
   functions the local `spanreed sleeper` commands call, which do all the
   validation themselves (see "Validation on receipt"). Building this means
   splitting each `_sleeper_*` handler into "run, return exit code and text" and
   "print it", so the remote path cannot drift from the local one, and moving into
   the "run" half every check argparse makes today.
3. The worker stores the result in the idempotency record, logs `done`, and hands
   the result to the main thread, which sends it. After a `create` or `retire`, the
   main thread sends a fresh `registry` frame **before** the result. Frames arrive
   in order on one pipe, so by the time the requester's CLI returns, the new
   sleeper is already mirrored there (or the retired one already gone), and `send
   --to agent-X@PEER` works straight away instead of after the next sync tick.

**`PATH` and `claude` on the target.** A local `create` resolves `claude` and
`spanreed` on the creator's `PATH` and records both, plus the `PATH`, for every
later wake. In a remote create, "the creator" is a bridge process. On the serve end
that is an ssh-launched command with a stripped `PATH`, which is why `conjoin`
already has to find the remote `spanreed` with an interactive-shell probe. So a
remote create resolves `claude` and `spanreed` on **the `PATH` recorded when
`remote-access on` was run** (R1, decision 6), normally by a person in a real shell
on this machine (any process running as the user can also write `path_env` itself;
decision 7). If that `PATH` finds neither, the create fails with the target's
message, naming `spanreed sleeper remote-access on` as the way to record it again.

The target is also where a wake from a remote `wake` starts tmux, so a wake
requested over the serve end has the same ssh-scope exposure as a wake caused by
mail arriving there (`KillUserProcesses`, under "Not verified yet").

#### Turning it on, at the managed machine

**Not a `conjoin` flag.** The serve end of a bridge is launched by the *other*
machine: `conjoin HOST` builds the remote command line itself (`bridge.py`,
`connect`, from `serve_cmd`). A flag on `conjoin --serve` would be chosen by the
peer, which would let a peer turn it on for itself. A flag on the connect end would
cover only one of the two roles, and whether it was on would then depend on which
machine happened to dial.

**One switch per machine** (decision 3), kept in this machine's `config.json`:

```
spanreed sleeper remote-access show   # on or off, when it was turned on, and the PATH recorded
spanreed sleeper remote-access on     # needs a person at a terminal; see below
spanreed sleeper remote-access off    # turn it off; takes effect on the next request
```

- **Off by default.** While it is on, **every machine conjoined with this one**, in
  either direction, may create, wake, sleep, retire and list this machine's
  sleepers. There is no list of peers, and nothing is keyed on a peer's host label,
  so a peer that claims a false label in `hello` gains nothing here that it
  wouldn't have under its real one.
- **It is read from disk for every request**, so `on` and `off` take effect on the
  next request with no bridge restart. An operation already running when it is
  turned off finishes. An unreadable or malformed `config.json` reads as `{}` today
  (`store.py`, `_read_config`), which here means **off**: the check fails closed.
- **It covers all five operations, `list` included** (R2, decision 6). `list` only
  reads, but a record holds `cwd`, `claude_args`, `path_env` and the last wake
  failure, which the registry does not carry.
- A requester needs no setting of its own to ask.

**`on` asks for a person at a terminal** (decision 5). This guards against
accidents, not against agents (decision 7). It refuses, changing nothing, unless
stdin is a TTY. It then prints what it grants, and turns the switch on only if this
machine's host label is typed back exactly:

```
$ spanreed sleeper remote-access on
Remote sleeper management lets EVERY machine conjoined with this one (adolin), in
either direction, create, wake, sleep, retire and list sleepers here. That means
any of them can RUN COMMANDS AS YOU on adolin, including a machine adolin dialled
with `conjoin` that has no ssh access to adolin, and any agent or process running
as you on such a machine. A remote create is not limited beyond a local one: it
can make a bypassPermissions sleeper and mark its folder trusted, pass an inline
--mcp-config whose command Claude runs when the sleeper wakes, or point
--debug-file at any file you can write.

Conjoining with someone means trusting their entire bus and network: a machine
conjoined only to one of your peers can reach adolin through it, if that peer's
switch is on too.

It stays on until `spanreed sleeper remote-access off`.
PATH recorded for remote creates: /home/u/.local/bin:/usr/local/bin:/usr/bin

Type this machine's label (adolin) to turn it on:
```

`off` and `show` need no TTY: turning it off is always safe. Running `on` again
records the current `PATH` again. `config.json` holds `sleeper_remote_access` as
`{"enabled": true, "enabled_at": "<UTC>", "path_env": "<PATH>"}`; anything other
than `"enabled": true` reads as off, and `off` removes the key. The label typed
back is this machine's hostname (`socket.gethostname()`), the label its `conjoin
--serve` end gives in `hello`; a `conjoin --label` override does not change it.

**What the TTY check does not stop.** It is protection against accidents, such as
a script or a stray command turning the switch on without anyone reading what it
grants. It is **not a boundary against agents or processes running as this user**,
and nothing in this design is (decision 7). The switch is a plain key in a file the
user can write: any such process can set `sleeper_remote_access` in `config.json`
with `jq` or `python -c` and never run `remote-access on`, and can set `path_env`
to a directory holding its own `claude` while it is there. That includes a session
acting on a bus message and a bypass-mode sleeper on this machine. Even through
the command, a process can get a terminal on purpose: type into a tmux pane with
`tmux send-keys`, or run it under `script(1)`. The owner accepted this for v1 (R10,
decision 8; decision 7 for why nothing stronger, such as a root-owned file, is
added).

**A refused request** gets `outcome: "refused"`, exit 4, and this text, so the
person reading it knows where the fix is:

```
[adolin] refused: remote sleeper management is turned off on adolin.
  To turn it on, run this ON adolin, in a terminal:  spanreed sleeper remote-access on
```

The target logs the refusal too.

#### What turning it on grants

**It grants running commands as this machine's user, to every conjoined peer.** A
remote `create` is checked exactly as a local one, and a local `create` limits
nothing its user chooses, because locally the creator is the owner. The claude-args
allowlist limits what the *sleeper* may be given beyond its permission mode; it was
never meant to limit the *creator*. So a peer can, with nothing more than a create
and a message:

- **create a `bypassPermissions` sleeper** (`PERMISSION_MODES`, `sleepers.py`) with
  `--trust` on any project folder, then mail it instructions;
- **pass `--mcp-config` an inline JSON string** (allowlisted; `claude --help` says
  it takes "JSON files or strings") naming a command, such as `sh`, which Claude
  starts at the sleeper's first wake, whatever its permission mode. That it starts
  without a prompt is reasoned, not measured;
- **pass `--debug-file PATH`** (allowlisted), which writes to any path the target
  user can write.

Decision 4 is to leave this uncapped and say so, here and in what `on` prints. The
argument that a peer reached over ssh could do all of this anyway holds for the
end that dialled. It does not hold for the other end: the machine this one
dialled often has no ssh path back at all (decision 1's Mac behind NAT), and for it
the switch is the only way in. Nor does any of this stop at the peer's owner: any
agent or process running as that user on the peer can write the peer's request
spool. The earlier draft called the grant "bounded" by the allowlist; it is not.

**It is roughly the access ssh already gives, made bidirectional** (decision 9). A
conjoin already implies that the dialling end can ssh into the other. The switch
gives about the same power, but in both directions: the end that was ssh'd into can
also manage the dialling end, which it may have no ssh access to. This is expected.

**Conjoining with someone means trusting their entire bus and network**
(decision 10). Trust is transitive: if A and B both have the switch on, a machine
conjoined only to B can reach A through B, by running commands as the user on B and
from there managing A's sleepers. This is by design and not limited. The example
is a lower bound: bus trust extends a third machine's reach beyond the switch,
because B's agents can act on its messages (and, per decision 7, can turn B's switch
on). That is what "trusting their entire bus and network" covers.

#### The audit log

Every remote operation the target sees is logged at
`<state>/logs/sleeper-ops.jsonl`, owner-only, append-only, one JSON object per
line, readable with `jq`. A request writes **at least two lines** with one
`request_id`: `received` (before anything runs, so an operation that crashes the
bridge still leaves a trace) and one outcome line. A refused, invalid or
expired-on-arrival request writes one line.

```json
{"ts": "2026-11-25T18:04:11Z", "event": "received", "peer": "kaladin", "role": "serve", "bridge_pid": 4242, "request_id": "3f1c…", "expires_at": "2026-11-25T18:04:40Z", "op": "create", "args": {"name": "X", "cwd": "~/git/proj", "permission_mode": "acceptEdits", "model": null, "idle_timeout": "15m", "trust": true, "claude_args": ["--mcp-config", "/home/u/servers.json"]}, "requested_by": "jmonk on kaladin (agent-planner)"}
{"ts": "2026-11-25T18:04:12Z", "event": "done", "peer": "kaladin", "role": "serve", "bridge_pid": 4242, "request_id": "3f1c…", "op": "create", "outcome": "ok", "exit": 0, "summary": "created sleeper X (agent-X), ASLEEP"}
```

- `event`: `received`, `done`, `refused`, `invalid`, `expired` (not run because its
  `expires_at` had passed, on arrival or while queued), `duplicate` (answered from
  the idempotency record without running again), `interrupted`.
- `peer` is the host label the peer gave in `hello`. It is self-asserted and
  identifies nothing; it is logged so a person can tell links apart.
- `args` is logged **in full**, including every `claude_args` value, since those
  are how a remote create runs commands here. They are paths and flags; spanreed never puts a
  credential in them.
- `requested_by` is **asserted by the requester and verified by nothing**, like a
  message's `from_agent`. It records which local user (and agent, if the CLI ran
  inside one) asked, so a person can follow it back; it is not evidence.
- `summary` is the first line of the operation's output.

The requester writes no log of its own; the reply it prints is its record, and the
target's log is the authoritative one. Rotation follows the activity log's answer
(none yet; see open-questions.md).

#### Frames

Two new kinds, sent by either end:

```json
{"kind": "sleeper-op", "request_id": "<32 hex>", "expires_at": "<UTC>", "op": "create", "args": {...}, "requested_by": "<free text>"}
{"kind": "sleeper-op-result", "request_id": "<32 hex>", "outcome": "ok", "exit": 0, "stdout": "...", "stderr": "...", "records": null}
```

The fields, the `args` for each `op`, and the validation rules are in
[protocol.md](protocol.md#pipe-frames). Both ends also add `"features":
["sleeper-ops"]` to `hello`.

#### Version skew

Frames carry no version, and that stays true. Two facts from the current code make
skew detectable without one:

- **An older receiver ignores an unknown `kind`** and records nothing (protocol.md,
  pinned by `test_an_unknown_frame_kind_is_ignored_and_leaves_no_note`). So an
  older target would drop `sleeper-op` silently, and the requester would wait out
  its whole timeout for an answer that was never coming.
- **An older receiver reads only `host` from `hello`** (`bridge.py`,
  `_handle_frame`; by reading the code, not pinned by a test). So an extra field
  in `hello` is safe to send to it.

So capability goes in `hello`: `"features": ["sleeper-ops"]`. The receiver records
it on its peer record as `peer_features`, **`null` when the field is absent**,
meaning "that peer's bridge is too old to say", per the existing rule that an
absent optional field reads as `null`, not as empty. Each bridge also records
its **own** list as `bridge_features`, because the requester has a second skew to
catch: the CLI may be newer than the `conjoin` process already running on its own
host, which would never read the spool. Step 1 of the spool checks both, and says
which machine to upgrade or which `conjoin` to restart:

```
spanreed sleeper wake --host adolin X: adolin's bridge does not support remote sleeper
management (its spanreed predates it). Upgrade spanreed on adolin and restart its end of
`conjoin`. Nothing was sent.
```

`hello` is the right place, not the `registry` frame, because what it states is a
property of the running bridge process, fixed for its lifetime, and known before
anything else is exchanged. Whether the peer has **turned remote access on** is
deliberately **not** advertised: the switch is re-read per request, and the refusal
reply is the authoritative answer.

A newer peer may later send an `op` this version doesn't know. The target answers
`outcome: "invalid"` naming the op, rather than ignoring it, since the frame kind
itself is one it understands.

#### Idempotency: the same request id never runs twice

Resending is how a request survives a dropped pipe, so the target must recognise a
resend. It keeps one **idempotency record** per request at
`<state>/sleeper-ops/in/<peer>/<request_id>.json`: the request (including its
`expires_at`), `received_at`, the pid and start time of the bridge process running
it, `state` (`queued`, `running` or `done`),
and, once done, the full result frame. Keyed by peer and id, because the id is the
requester's and only unique on its side.

Every change to a record is decided on the record as it is, not as some process last
read it: reading a record and writing its next state is one step, under an
exclusive `flock` on `<state>/sleeper-ops/in/<peer>/.lock` (never deleted, like a
sleeper's lock). That covers a new record, a re-queue from a dead process, the step
to `running`, the step to `done`, and the bridge's start-up prune of records older
than seven days; the lock is never held while an operation runs.
It matters because one peer can have two live bridge processes here: when two hosts
are conjoined both ways, each has a bridge for the other in each direction. Without
it, both could find a request queued under a dead process and both take it over, or
a second worker could start one already running. A new record is also written
create-only, as a backstop. The worker runs a request only if its record is
`queued` under its own bridge process; anything else it leaves to the process that
holds it.

On a `sleeper-op` whose record already exists, the record decides, whatever the
frame's `expires_at` says:
- **`done`**: send the stored result again; log `duplicate`. Nothing runs.
- **`queued` or `running` in a live process** (this one, or an earlier bridge process for the
  same peer that hasn't exited yet): send nothing now. This bridge watches the
  record and sends the result when it turns `done`.
- **`running` in a dead process**: the operation was interrupted partway. Answer
  `outcome: "interrupted"`, exit 1, with the text "the bridge on adolin ended while
  running this; its effect may be partial. Check `spanreed sleeper list --host
  adolin`." **It is not run again** (R8, decision 6).
- **`queued` in a dead process**: it never started. Answer `outcome: "expired"` if
  its `expires_at` has passed; otherwise queue it again under this process.

On a `sleeper-op` with **no** record and an `expires_at` that has passed by the
target's clock, the target writes a `done` record with `outcome: "expired"`, logs
`expired`, and answers that. It never runs it. Because no request lives longer than
600 s and records are kept seven days, a request whose record was pruned can only
arrive long expired, so it cannot run twice (see "Every request expires").

Why this matters for more than tidiness: the operations are **not** naturally
idempotent. A `create` that succeeded, resent after a drop and run again, would
fail with "name already held" and report a failure for something that worked.

#### When things break

- **The bridge drops while a request is in flight.** On the requester, the file
  stays `.sent`. When the bridge reconnects (`conjoin`'s reconnect loop), the main
  thread **resends every `.sent` request that has not expired**, and the target
  answers from its idempotency record. A `.sent` past its `expires_at` is never
  resent, whether or not its CLI is still alive. If the CLI's timeout passes first, it says
  "sent, outcome unknown" (exit 3). On the target, a bridge that is told to stop
  waits for its in-flight operation to finish and stores the result before
  exiting, bounded by that operation's own limit (at most `RETIRE_WAIT_S` plus a
  margin). A bridge killed outright leaves a `running` record, which the next
  duplicate turns into `interrupted`.
- **The target host is suspended or unreachable.** The requester's watchdog ends
  the link after `recv_timeout`. A request not yet sent fails with "not sent"; one
  already sent reports "outcome unknown". If the host wakes and the link returns
  while the CLI is still waiting, the resend delivers the answer.
- **The target sleeper is asleep.** Each operation behaves as it does locally:
  `wake` launches it, `sleep` answers "already asleep", `retire` retires it at once,
  and `list` shows `ASLEEP`. The target being asleep never blocks a request.
- **The target sleeper is awake and mid-turn.** `retire` asks it to sleep and waits
  up to 60 s on the target, as locally, then fails with the local message. The
  requester's 90 s default covers that wait.
- **Duplicated on reconnect.** Covered by the idempotency record above.
- **The requesting CLI is killed** before its timeout. Its `.json` or `.sent` file
  stays, but the bridge sends it only until `expires_at` and then renames it
  `.abandoned` (see "Every request expires"). It never fires after the timeout the
  person chose.
- **A second operation while one is running.** It waits on the target's queue
  while the link is attached (R6). While the link is reconnecting or stopped, it
  fails at once on the requester (spool step 1), naming the state.
- **Issue [#34](https://github.com/Monkopedia/spanreed/issues/34)**, built in #120:
  mail to a peer's mirrored agents queues while `conjoin` is reconnecting and is
  refused once no `conjoin` for that host runs. Management never resolves through
  the registry: the target looks the sleeper up by name in its own records, so #34
  doesn't change any operation here. While the link is reconnecting or stopped, a
  remote sleeper can't be managed (by design, see "Which link state counts as
  down"), even though its mail queues while reconnecting. After a
  remote `create`, the new sleeper is addressable here as soon as the result
  arrives, because of the registry frame sent ahead of it, and from then on its
  mail follows the same rule as every other mirrored agent.

#### Validation on receipt

**The target trusts nothing the requester checked.** The requester may be older,
newer or modified, and it skips argparse's checks entirely, since only `args`
crosses the pipe. Every check is therefore made in the function both paths call
(`sleeper_ops.py`), so the local and remote paths share one set. Until #86 stage 1
the permission mode was checked only by argparse's `choices=PERMISSION_MODES`
(`cli.py`); argparse still checks it, for its help text, but no longer alone. The
`name` given to `wake`, `sleep` and `retire` is checked too, since it becomes a
file name and a local command never needed to check it:

- **`request_id`** becomes a file name on both machines, so it is checked like an
  agent id is (#12): exactly 32 lowercase hex characters. A `sleeper-op` with a bad
  id gets **no reply**, since there is no safe name to reply under; it is logged as
  `invalid` and noted on the peer record, and the requester's CLI times out with
  "not answered".
- **`expires_at`** must parse as a UTC timestamp no more than 600 s plus the clock
  allowance (30 s; R9, decision 8) ahead of the target's clock. Missing or out of range is
  `invalid`; passed is `expired`, as above.
- **`op`** must be one the target knows, and **`args`** must have exactly that op's
  fields with the right types; otherwise `outcome: "invalid"` with the reason.
- **`name`** goes through `validate_agent_id`, as locally.
- **`cwd`** must be absolute or start with `~`, checked on the string as received,
  before `~` is expanded; a relative path is refused rather than resolved against
  the bridge's working directory. Then it must be a directory, as locally.
- **`permission_mode`** must be one of `PERMISSION_MODES`, the static list taken
  from `claude --help` (Claude Code 2.1.283) in `sleepers.py`. It is not checked
  against the installed `claude`.
- **`claude_args`** go through `check_claude_args`, the same allowlist as a local
  create, and **`idle_timeout`** through `parse_duration`.
- **`trust`** goes through `trust_folder`, with its home-folder refusal.

Failing any of the checks from `name` on is `outcome: "failed"`, exit 1, with the
message the local command prints. Like every frame, a `sleeper-op` the receiver
cannot model is dropped and noted and never stops the reader (the #55 rule). None
of these checks narrows what a valid create can do (see "What turning it on
grants").

#### Not verified yet (remote management)

Nothing here has been built or run. To measure in the implementation PR:
- **The `PATH` a remote create sees** on each end: what the serve end's ssh-launched
  environment holds on kaladin, adolin and the Mac, and whether the `PATH` recorded
  by `remote-access on` finds `claude` from there.
- **What happens to the serve end when ssh drops.** The design assumes the serve
  process sees EOF and can finish an in-flight operation before exiting. Whether
  sshd sends it `SIGHUP` first, and how long it then has, is unmeasured.
- **`--trust` from a bridge process.** It writes the target user's
  `~/.claude.json`. Expected to behave as locally, since it is the same user and the
  same file, but not run.
- **A remote wake through the serve end on a `KillUserProcesses=yes` host.** The same
  open item as for mail-triggered wakes ("Not verified yet" above).
- **The registry-before-result ordering** making a new remote sleeper addressable
  the moment `create --host` returns.
- **An older peer ignoring `features` in `hello`.** Read from the code, not tested
  against an actually-older binary.
- **macOS as the target**, where none of this, and nothing about sleepers, has been
  run.

### Adopting a running agent

**Status: designed, not built.** Nothing in this section has been run. Facts below that
come from reading the code or looking at this machine say so. Anything that needs a
measurement is under "Not verified yet (adoption)". What was observed on this machine
is in [findings.md](findings.md#observations-for-adopting-a-running-agent-2026-11-25),
not here. The points the request does not settle are A1–A15 in
[open-questions.md](open-questions.md#adopting-a-running-agent-as-a-sleeper-designed-not-built),
each with a recommendation. This section describes the design **as recommended**, and
names the question each choice depends on.

**The request.** The owner's request of 2026-11-25, as main-coordinator relayed it.
It is a relay, not a comment from his own account:

> let 'spanreed sleeper create' ADOPT an existing running agent seamlessly, so the
> fleet can move idle repo agents to sleepers and free their RAM for builds. Two gaps
> today: (1) create always mints a new session UUID, so add an option to supply an
> EXISTING session id, so the first wake uses '--resume <id>' rather than
> '--session-id' and the agent keeps its conversation; (2) the agent_id is always
> agent-<name>, so add a way to keep the existing id (e.g. agent-eac78acd for hauler),
> so its inbox, cursor and every address that already points at it keep working.
> Today's only workaround is --name eac78acd, which makes the display name the hex.
> Also think through the handover: the existing live session must exit cleanly before
> the sleeper takes over the same session id, with no window where both run or mail is
> missed (does it adopt the existing inbox cursor?). Pilot targets once it lands:
> hauler, sdbus-kotlin, lsp-kotlin.

The request settles three things: the conversation is kept (the first wake resumes it),
the bus id is kept and the display name is not the hex, and the handover has no double
run and no missed mail. The rest is the author's design.

**The pilots** (hauler, sdbus-kotlin, lsp-kotlin) are listed with their ids and cwds in
[findings.md](findings.md#observations-for-adopting-a-running-agent-2026-11-25). What
matters for the design: all three have an id derived from their cwd, and all three run
interactively in panes of the owner's own `claude` tmux session.

#### What already works

- **The runner already resumes any conversation it finds.** It passes `--resume` whenever
  `find_transcript(session_id)` finds a transcript, and `--session-id` only when it finds
  none (`sleeper_runner.run`). So gap (1) needs only `create` to accept an existing id.
  The runner does not change.
- **Keeping the id keeps the inbox and every address.** The inbox is
  `inboxes/<agent_id>.jsonl`, and every address resolves to that id: `agent-eac78acd`,
  the name `hauler`, and `agent-eac78acd@kaladin` as peers mirror it.
- **There is no cursor to adopt.** `cursors/` is written only by Codex workers and
  `conjoin` bridges (`codex_worker.py`, `bridge.py`). A Claude session keeps no cursor:
  its Monitor is `tail -n 0`, and the only record of what it received is its transcript.
  The transcript is what the read-check ("What counts as read") already reads, so adoption
  takes its starting point from there. `cursors/` is not touched.

#### The commands

```
spanreed sleeper create --adopt AGENT [--end-session] --permission-mode MODE [--name N] [--model M] [--idle-timeout 15m] [--trust] [-- CLAUDE_ARGS...]
spanreed sleeper create --name N --cwd DIR --session-id UUID [--agent-id ID] --permission-mode MODE [--model M] [--idle-timeout 15m] [--trust] [-- CLAUDE_ARGS...]
```

The second form is the primitive. The first one fills its values in from a running or
recently running session (A3).

- **`--session-id UUID`** adopts that conversation. Its transcript must exist and be
  readable in the known record shape, because the read-check is what makes adoption
  lossless, and a transcript the check cannot read would make every line unread. Its
  `cwd` must also match `--cwd`: the `cwd` of the transcript's first record of known
  shape must equal the resolved `--cwd`. That is the directory the conversation was
  started in, which is where Claude Code files it, and the runner launches in `--cwd`.
  No other sleeper record may hold the same session id.
- **`--agent-id ID`** keeps that bus id. It must pass `validate_agent_id(...,
  qualified_ok=False)`, so it is local and legal. No sleeper record may already hold it;
  that is checked on the record's `agent_id` field, not on its name (see "Name and id
  come apart"). The id's registry row, if it is live, must belong to the session being
  adopted, checked as in step 1 of "The handover". Any other live holder is refused, as
  in #80. Without `--agent-id`, the id is `agent-<name>` as today.
- **`--adopt AGENT`** takes an id or a display name, resolved as `send --to` resolves
  it, and reads everything else from the session behind it. The registry row gives the
  id, the display name and `pid`/`pid_start`. Claude Code's per-process file
  `~/.claude/sessions/<pid>.json` gives `sessionId` and `cwd`. That file is an internal
  format, not a promised one; what was observed of it is in findings.md (above).
  Adoption uses it only when its `procStart` matches the row's `pid_start`. When the file is missing, unreadable or does not match,
  `--adopt` refuses and says to pass `--session-id`, `--agent-id` and `--cwd` instead.
  It prints every value it took.
- **`--name`** defaults, under `--adopt`, to the session's display name if
  `agent-<name>` passes `validate_agent_id`. Otherwise it must be given. It must not be
  held by an existing sleeper, or as an id or display name by any present agent other
  than the session being adopted.
- **Not copied:** `--permission-mode` is required, as on every create (decision 2).
  `--model` and `CLAUDE_ARGS` are never read from the session. `create` prints the model
  the transcript last ran as a hint, because a different `--model` changes the model the
  conversation continues on.
- **`--host PEER` with any adopt flag is refused** in v1 (A11).

Everything else is checked exactly as in a plain `create`: the permission mode, the
claude-args allowlist, `--idle-timeout`, `--trust` and its home-folder refusal, and
resolving `claude` and `spanreed` on the caller's `PATH`.

#### Name and id come apart

Today a sleeper's `agent_id` is always `agent-<name>`, and several sites rely on that
rather than reading the field. An adopted record keeps its old id under a readable name
(record `hauler`, id `agent-eac78acd`). So:

- **The record is still keyed by its name.** The file and lock are `sleepers/<name>.*`,
  and every `spanreed sleeper` subcommand takes the name. That keeps `wake hauler`,
  `attach hauler` and `retire hauler` readable. Keying by the id's suffix instead would
  reproduce the `--name eac78acd` workaround the request rejects.
- **Every lookup from an id to a record reads the `agent_id` field.** That covers
  `StateStore.sleeper_for` (today it strips `agent-` and reads the file of that name),
  `sleeper_ids` (today it lists file names), and `sleeper wake`'s call to
  `wake_if_needed(sleeper_agent_id(name))`. The cost: `sleeper_ids` parses each record
  instead of listing a directory. Records are small, and a fleet has tens, not
  thousands. For a record created before this change, `agent_id` is `agent-<name>`, so
  every answer is unchanged.
- **`create` refuses an id that any record holds**, whatever that record's name. Two
  names can now map to one id: a later `create --name eac78acd` would otherwise mint
  `agent-eac78acd` a second time.
- **The woken session must speak as the record's id.** The runner sets
  `SPANREED_AGENT_NAME=<name>`, and the identity rules then mint `agent-<name>`. For an
  adopted record that would be `agent-hauler`, a different agent with an empty inbox.
  Recommended (A7): the runner (`sleeper_runner.py`, where it sets
  `SPANREED_AGENT_NAME` today) also sets `SPANREED_AGENT_ID=<record.agent_id>`. It is
  validated like any id and must be unqualified, and it is set on every launch, so a
  record whose id is `agent-<name>` behaves exactly as today. **Two functions mint an
  id from `SPANREED_AGENT_NAME`, and both must read `SPANREED_AGENT_ID` first**
  (read from the code):
  - `identity.derive_agent_identity` (`identity.py:35`). Reached by the SessionStart
    hook (`_cmd_session_start`, `cli.py:847`), by every MCP tool that asks who it is
    (`mcp_server.py:274`, `291`, `311`, `363`), by `register`'s default
    (`cli.py:145`), and by `_ensure_registered` for the name (`cli.py:584`).
  - `identity.session_agent_identity` (`identity.py:84`). It returns on
    `SPANREED_AGENT_NAME` itself (precedence 1) before it ever calls
    `derive_agent_identity`, so fixing only the first function leaves this one minting
    `agent-hauler`. It is the CLI's path, through `_self_identity` (`cli.py:75`): the
    inbox Monitor `inbox-watch` (`cli.py:416`), `send`'s default sender
    (`cli.py:365`), `agent-id`, and the other self-addressed commands (`cli.py:592`,
    `614`, `640`). A miss here would point the woken sleeper's Monitor at the wrong
    inbox.

  The implementation PR should route both through one helper and test each entry point
  above in a woken adopted sleeper. Three readers use `SPANREED_AGENT_NAME` as the
  **record key**, not as an id, and stay as they are: `request_sleep`
  (`mcp_server.py:330`), the Monitor's replay (`cli.py:445`) and the turn hooks'
  `_activity` (`cli.py:1065`).

#### The handover

Three shapes were considered. For each one, the deciding questions are whether two
Claude processes can run the conversation at once, and what happens to mail that
arrives while control changes hands.

**A. Refuse while the session is live; someone ends it first, then adopts.**
- *Double run:* none, provided the liveness check is right. The check is the registry
  row's `pid`/`pid_start`, which is the session's `$CLAUDE_PID`. A `claude --resume`
  started by hand in another terminal is invisible to that check (see A9).
- *Mail in the gap:* between the exit and the adopt, the row is stale. A send to
  `agent-eac78acd` is accepted and queued (CLI exit 3). A send to `hauler` **is
  refused**, because names resolve only among present agents (#55). Queued mail lands
  after the transcript's last delivery, so it counts as unread (see "Where `read_through`
  starts"). But `create` launches nothing, so it waits for the next send or a `sleeper
  wake` (A6).
- Simple, and it needs no new process. But it is not seamless: it needs a person or a
  script to end the session, and senders who use the name get errors in the meantime.

**B. The live session adopts itself, and a waiter takes over when it exits.** The session
runs `create --adopt` from its own Bash tool. The record is written `awake` at
generation 0, with `claude_pid` set to the live session and `runner_pid` set to a
detached waiter. When the pid exits, the waiter does steps 4 and 5 of "Committing to
sleep".
- *Double run:* none. A wake sees a live runner and does nothing. If the waiter dies,
  the orphan rule (`runner` dead, `claude_pid` alive) blocks a resume.
- *Mail in the gap:* there is no gap. The live session's Monitor delivers until it exits.
  After that the waiter's step 4 sees everything as it would for a runner.
- **Does the waiter fit "no persistent process"?** As a lifetime, yes. It is a runner
  whose Claude it did not launch, and it lives exactly as long as that agent is awake,
  which is the lifetime the decision already accepts for runners. But nothing ends the
  live session. Its hooks do not report turns, because `SPANREED_SLEEPER` is not in its
  environment, so the waiter cannot tell idle from busy, and it waits until a person
  exits. Ending it would take either reading turn boundaries from the transcript or a
  plugin hook change. The bare orphan rule is also the wrong signal here: as built, it
  records a wake *failure* and sets `needs_input` on every send. So B would need its own
  marker and wording.

**C. `create` ends the live session itself, then commits, all in one bounded call.**
This is **recommended for v1 and the pilots** (A1). Run from outside the session, by a
person or by the coordinator (who may is A14), C does what the runner does at sleep,
with the `create` process acting as the runner for generation 0. The difference from a
runner is that the live session was not started by spanreed: its hooks report no turns
and no `Stop.background_tasks` (it has no `SPANREED_SLEEPER`), so `create` has to judge
"idle" from outside (A2, A13), and its Monitor keeps delivering mail until it is stopped.

1. **Validate**, then take the sleeper lock. Refuse unless all of these hold:
   - If the id's row is live, its pid is the session being adopted: that pid's
     `~/.claude/sessions/<pid>.json` names this `sessionId`, and its `procStart`
     matches. A live session is ended only with `--end-session` (A2). Without that
     flag, `create` refuses while the session is live and says how to proceed.
   - **No other session answers this id or runs this conversation.** The row holds
     only the latest pid, and two sessions in one cwd share an id (protocol.md), so the
     row alone cannot show a second session. `create` refuses when (a) any process
     other than the adopted session's own Monitor is tailing this id's inbox file (the
     Monitor is `tail -F <inbox>`, so this finds every listening session, however its id
     was minted), (b) any other live `sessions/<pid>.json` names this `sessionId` (A9's
     check, run here too), or (c), for an id derived from a cwd, any other live
     `sessions/<pid>.json` has that cwd. All three are best-effort; (a) is the one that
     does not depend on Claude Code's internal files.
   - **The caller is not a descendant of the session.** Step 5 would signal the
     `create` process itself. Self-adoption is shape B.
   - **No pending abort report under this name** (`listing: pending`, "The abort
     path"). Committing would remove a report whose listing was never made; `create`
     names `spanreed sleeper adopt --recover NAME`, which finishes it.
   - **The session looks idle** (A2): its transcript has not been written in the last
     60 s, and its last record ends a turn (no `tool_use` waiting for its result, no
     user record waiting for a reply).
   - **No background work is in flight** (A13, as recommended: adoption waits for
     background work, it does not stop it). The live session has no
     `Stop.background_tasks` to read, because its hooks don't report turns, so this is
     judged from outside, by two checks:
     - **Primary: a launch with no completion, in the session's transcripts.**
       Every background launch writes a `tool_result` in the transcript: "Async agent
       launched" for a background agent, "Command running in background with ID: …"
       for a background Bash. Every completion comes back as a `<task-notification>`
       (a `user` record, or a `queued_command` attachment) carrying the launch's
       `<tool-use-id>` and a `<status>` (`completed`, `killed`, `failed` or `stopped`;
       a background agent stopped with TaskStop gets `killed` with its id). A launch
       whose `tool_use_id` has no matching notification is work in flight. Only
       launches written since the current process started count: the session file's
       `procStart` (equal to the row's `pid_start`) gives that time, and a resumed
       conversation can hold launches from an earlier process, which are not running.
       The same pairing is applied to subagent transcripts
       (`<project>/<session-id>/subagents/agent-*.jsonl`), for work a subagent
       launched. The pairing was measured by the reviewer of #129 on one session's
       transcript and on the status counts across every project (findings.md).
       Two kinds of launch need more than this:
       - **Monitors.** Launches by the Monitor tool are **left out of the pairing**,
         because most Monitor notifications carry no `<tool-use-id>` (19 of 21 on the
         transcript the reviewer measured), so a finished Monitor would often look
         unfinished, and adoption would be refused for as long as the process runs.
         A Monitor is a shell command, so while it runs it is a descendant process of
         the session, and the backstop below judges it: any running Monitor other than
         the inbox's (`spanreed inbox-watch`, excluded by its command) refuses the
         adoption, and `create` names its command and says to stop it (TaskStop) or
         wait for it. A Monitor that has ended has no process and blocks nothing.
       - **Work launched before a `/clear`.** After `/clear` the same process writes a
         new transcript, under a new session id. A background agent launched before the
         `/clear` and still running is not in the new transcript, and it runs inside
         the `claude` process, so the backstop cannot see it either. A miss here costs
         what any miss costs (below): step 5 ends the process, and the agent's work and
         result are lost. So the pairing reads **every transcript in the project folder
         last written at or after `procStart`**, with their subagent transcripts, and
         pairs launches and notifications across all of them together (a notification
         for a launch made before the `/clear` may be written to the new transcript).
         Any launch this process made is in one of them, because it was written after
         the process started. Transcripts of other sessions in the same folder can only
         add launches, so they can cause a refusal but never a miss. If any of those
         transcripts cannot be read, `create` refuses. The alternative, simpler and
         coarser, is to refuse whenever the adopted transcript began after the process
         started (that is, the session has run `/clear` or switched conversations),
         until the session is restarted (A13).
     - **Backstop: descendant processes.** Any descendant of the session's pid other
       than its MCP servers, its inbox Monitor and their children (a background shell,
       a build, a foreground tool's process, any other Monitor).

     Either check finding something refuses, and `create` says what it found (the
     launch's tool and command or description, or the process). **A miss is not
     safe:** work that neither check sees is killed at step 5, a background shell by
     SIGTERM/SIGKILL and a subagent with the `claude` process it runs in. Its result
     never comes back, and the resumed conversation holds a launch with no completion.
     Only a false positive fails safe, by refusing.

   Then record the **inbox snapshot** `S`: the inbox's current length, as the
   `InboxPosition` that `read_through` already uses. Only lines at or before `S` can
   ever be treated as history ("Where `read_through` starts").
2. **Write the record.** First, still before the write, install the interrupt handler
   (see "If `create` is interrupted" below), so that no signal that arrives once the
   record is on disk goes unrecorded. The handler only records the signal; `create`
   acts on it at the checkpoints listed there, all outside the lock. Then write: `state=sleeping`,
   `generation` 0, `runner_pid` set to this process, `claude_pid`/`claude_pid_start`
   set to the live session, `S`, and `read_through` set to a **provisional** position
   computed now (see below). The registry row keeps the live pid, with the sleeper
   marker laid over it from the record, so name lookup keeps working. From this moment
   a sender sees `sleeping` with a live runner, and only appends. A wake, by mail or by
   hand, does nothing. Then **release the lock.**
3. **Stop deliveries to the live session**, in this order:
   1. Find the session's Monitor: a descendant of its pid running
      `tail -n 0 -F <this inbox>`.
   2. **Take the lock again**, and record the inbox's length as the **freeze mark** `F`.
   3. Still under the lock, write the Monitor's pid, its start time and `F` into the
      record (`adopted.frozen_monitor`). The write is the usual atomic replace, and it
      has completed before the next step begins.
   4. Only then, still under the lock, send the Monitor `SIGSTOP`, and release the lock.

   The write comes first so that there is no moment when the Monitor is stopped and the
   record does not say which one. A `create` killed between the write and the `SIGSTOP`
   leaves a record naming a Monitor that is still running, and the recovery's `SIGCONT`
   to a running process changes nothing (see "If `create` is interrupted"). From here on,
   mail is appended to the inbox and nothing hands it to the live session. `SIGSTOP` is used
   rather than killing the Monitor so that an abort (step 4) can undo it: `SIGCONT`
   resumes `tail` at its file offset. But a stopped `tail` buffers nothing and reads
   nothing; on `SIGCONT` it reads every line it had not yet read at once, and emits them
   as **one burst** (measured by the reviewer of #129, findings.md). Claude Code merges
   such a burst into one event and cuts it at about 3,000 characters, and the lines past
   the cut never reach Claude (Test 9). So an abort can make the live session miss mail;
   see "The abort path". No Monitor found means none is delivering, and the step is
   skipped. More than one is refused at step 1.
4. **Re-check idle, after a quiet period.** Wait `Q` (recommended: 10 s, so a Monitor
   event queued just before the freeze has time to start its turn), then run the step-1
   idle and background-work checks again, requiring no transcript write since the
   freeze. **If anything is active, abort** (A2), as "The abort path" below describes,
   and exit non-zero saying the session became busy.
5. **End the live session**, as in "The exit": list its descendants, then, **under the
   lock, write the phase marker** `adopted.ending`: the time, and the descendants just
   listed, each by pid and start time. Release the lock. Only after that write, send
   SIGTERM, then SIGKILL after 15 s, then SIGKILL to any descendants still running whose
   start time is unchanged. The stopped Monitor is one of them; SIGKILL ends a stopped
   process. It is not this process's child, so "gone" is polled by pid and start time.
   The marker is what lets a recovery tell "`create` died before it began ending the
   session" (undo the freeze) from "`create` died while the session was ending" (finish
   the commit); without it the two look the same on disk.
   Background work is **not** knowingly stopped: by step 4 none was found in flight
   (A13). What a miss costs is stated at step 1.
6. **Under the lock, commit**: compute `read_through` against the now-complete
   transcript and `S`, rewrite the row as a sleeper row (`pid` 0, `pid_start` −1,
   display name from the record), and set `state=asleep`. If any line after
   `read_through` is unread, release the lock and wake it through the ordinary wake
   path (generation 1, `--resume`, in a new tmux session; A6). A sender that wakes it
   first in that moment makes this wake a no-op.
7. Print what happened: the values used, `S`, `read_through`, the lines that will be
   replayed and the lines treated as history.

**The lock is not held across steps 3 to 5**, as it is not across the runner's own
exit. Holding it for up to 25 s would block every sender to this id. Steps 3 and 5 take
it only for their record writes (and step 3 for the `SIGSTOP` that follows its write).
Exclusion in the rest of that window comes from the record: `sleeping` with a live
`runner_pid`, which no wake acts on.

- *Double run:* none. The record exists, with a live runner, before the session is
  signalled, and nothing can wake the sleeper until step 6.
- *Mail that is never seen, when the adoption commits:* none. Every inbox line after `S`
  that the transcript does not show as delivered is replayed, one line at a time, as
  every sleeper replay is. Lines appended after the freeze cannot reach the live
  session, so all of them are replayed unless they were delivered before it. (A line
  already in the Monitor's pipe when `SIGSTOP` lands can still be delivered; the turn
  it starts is what the step-4 re-check sees.) Before step 3 the live session receives
  mail normally. Mail the live session had queued but never turned into a turn has no
  delivered record, so it is replayed. A line before `S` that the live session missed
  is history (A5), and `create` lists it.
- *Mail when the adoption aborts:* see "The abort path". The guarantee there is weaker:
  nothing is dropped **silently**, but mail can miss the live session.
- *What is not guaranteed: a turn cut off by step 5.* A message that reached a turn
  counts as read once that turn has a real assistant record, so if step 5 ends that
  turn, the message is not replayed and the work on it is lost. Steps 3 and 4 make
  this unlikely: after the freeze no new mail can start a turn, and the re-check
  aborts if one is running. They cannot rule it out. The idle checks are judged from
  outside (A2), a foreground tool call may write nothing to the transcript while it
  runs, and a turn can also start from something other than mail (a person typing in
  the pane, a background task finishing) in the moment between step 4 and step 5.
  The claim is therefore: **when an adoption commits, no message goes unseen, and a
  message is lost mid-work only if a turn starts or runs undetected through the step-4
  re-check.**
- *Bounded:* about `Q` plus an exit, at most 15 s plus the escalation. An abort adds
  the wait `W` below. No process outlives the command, so it fits "no persistent
  process" without needing the runner analogy.
- *If `create` dies*: see "If `create` is interrupted" below.
- *A session that has already exited* (shape A) is the same command without steps 3
  to 5.
- *The risk is ending a session mid-turn.* Test 8 measured SIGTERM only at idle. C
  therefore refuses, twice, while the session looks busy (A2).

**The abort path.** Step 4 takes it when the session is busy, a checkpoint takes it
when `create` was told to stop ("If `create` is interrupted"), and the recovery below takes it when `create`
was killed before the phase marker. The Monitor must be resumed, because it is the live
session's only way to hear mail, and resuming it releases every line it had not yet read
as one burst. That is every line appended since the freeze mark `F`, and also any line
appended just before `F` that `tail` had not read when `SIGSTOP` landed. Test 9 measured
that a burst is merged into one event and cut at about 3,000 characters, so a handful of
long messages during a 10–25 s freeze is enough to lose some. Nothing in the live session
re-sends them, and the agent is not told which ones it missed. There is no way, from
outside the session, to make `tail` deliver them one at a time. So the abort path does
not promise delivery. It promises that every line is accounted for:

1. `SIGCONT` the Monitor (if step 3 stopped one).
2. Under the lock, first write the **abort report**, `sleepers/<name>.adopt-aborted.json`,
   marked `listing: pending`, with `S`, `F`, `adopted.frozen_monitor`, the transcript
   path, the reason, the time, and the pid and start time of the process running the
   abort; then, still under the lock, delete the record. That removes the sleeper marker
   and leaves the live row as it was. The order matters: from the moment the record is
   gone, the pending report is what names `S` and `F`, so a death during the wait below
   leaves something that says the lines after `S` are unchecked.
3. Wait `W` (recommended: 30 s, so the burst's turn has time to write a real assistant
   record). Then take the inbox's length as the **listing mark** `L`, and run the
   read-check on every line after `S` up to `L`. The listing starts at the inbox snapshot
   `S`, not at `F`, so that a line `tail` had not read when it was stopped is covered too.
   Lines between `S` and `F` were most likely delivered one at a time and show as such;
   listing them costs nothing.
4. Report **every line after `S` up to `L`**, by line number, `msg_id`, sender and
   length, each marked "delivered" or "not shown as delivered", in three places: the
   command's output (when there is one; a recovery started by the wake path has none),
   the activity log (rule 7), and the abort report, which step 4 completes: under the
   lock, it rewrites the report with `listing: complete`, `L` and the undelivered lines.
   `sleeper list` shows a complete report until it is dismissed (`spanreed sleeper adopt
   --dismiss NAME`) or the next adoption under that name commits. It is a separate file
   because the record is gone after step 2, and because the activity log is opt-in, so it
   alone would not reliably reach a person. The report says that the undelivered lines are still in the inbox,
   where the agent's `recv_messages` can read them, and that a person can forward them.
   `create` then exits non-zero.

**A pending report is never dropped.** If the process that wrote it is dead (pid and
start time), whatever finds it finishes it: the read-check needs only the inbox and the
transcript, both named in the report, so any process can do steps 3 and 4. A finisher
first sends `SIGCONT` to `frozen_monitor` if its pid and start time still match (a no-op
if step 1 already did it), waits out whatever is left of `W` since the report's time,
then lists up to the inbox length it sees and completes the report under the lock, if it
is still pending when re-read there (two finishers write the same account; the first
wins). The finishers:
- `sleeper list` shows every pending report as "listing not finished: lines after `S`
  unchecked", with whether its writer is alive. If the writer is dead and `W` has passed,
  `list` completes it before printing. It does not wait for `W`; a younger one is shown as
  pending, with the command below.
- `spanreed sleeper adopt --recover NAME`, with no record but a pending report whose
  writer is dead, finishes it, waiting out `W`.
- `--dismiss NAME` refuses a pending report and names `--recover`. A new adoption under
  that name is refused at step 1 while a pending report exists, for the same reason: its
  commit would remove a report whose listing was never made.

Lines appended after `L` are not listed and need not be: the Monitor was resumed at
step 1, and the burst it released ends before them, so each reaches the live session as
its own event, as any mail does.

The guarantee on abort is therefore: **every line appended after `S`, up to the moment
of listing, is either shown as delivered in the live session's transcript within `W`, or
listed by line number and `msg_id` in the abort report, the activity log and (when
`create` ran the abort itself) its output.** It is not guaranteed that the live session
receives it. A line listed as "not shown as delivered" may still be in a turn that has
not finished. A later adoption of the same agent would class such a line as history if
the session went on to receive later mail (it is before that adoption's `S` and its
high-water mark), and A5's listing names it again. Whether the abort should also re-feed
these lines, and whether aborting is right at all, is A2.

**If `create` is interrupted.** `create` installs a handler for SIGINT, SIGTERM and
SIGHUP at one point only: at step 2, **before it writes the record**. Before that, a
signal has its default effect: `create` ends, its flock goes with it, and no record
exists.

**The handler only records the signal.** It sets a flag (the first signal's number;
later ones change nothing), and returns. It takes no lock, deletes nothing, sends no
signal and makes no blocking call. This matters because the handler runs in `create`'s
main thread, between two bytecodes of whatever `create` was doing, and `create` holds
the sleeper lock at step 1, across step 2's write, across steps 3.2 to 3.4, and across
step 5's marker write. `sleepers.lock()` opens a new fd and `flock`s it on every call
(`sleepers.py`), and a second `flock(LOCK_EX)` from the same process on a new fd waits
for the first (measured by the reviewer of #129, findings.md). A handler that took the lock inside
one of those windows would wait forever while holding it, and every sender to the id
would hang in `_wake` behind it.

`create` acts on the flag at these **checkpoints**, each of which is reached with the
lock not held:
- **K1:** right after step 2 releases the lock.
- **K2:** after step 3.1 finds the Monitor, before step 3.2 takes the lock.
- **K3:** right after step 3.4 releases the lock.
- **K4:** throughout step 4: the wait `Q` polls the flag (at least every 0.5 s), and the
  flag is checked again after the idle re-check.
- **K5:** after step 5 lists the descendants, immediately before it takes the lock to
  write the phase marker.

At a checkpoint with the flag set:
- **K1 or K2** (`adopted.frozen_monitor` not yet written): nothing has been stopped.
  `create` takes the lock, deletes the record, releases it, writes no abort report (no
  mail can have missed the session), and exits with the signal's status.
- **K3 to K5** (`frozen_monitor` written, phase marker not): `create` runs the abort
  path, with the reason "interrupted by signal N", and exits with the signal's status.
  The session is not left deaf.

A signal that arrives inside a locked window is therefore acted on at the next
checkpoint, which follows the window's release directly. The windows are a record write
and, in step 3, one `SIGSTOP`. A signal received after the K5 check
comes too late to abort: the session is being ended. `create` finishes step 5 and the
bounded commit, then exits with the signal's status (A15 (i)). A signal received while
the abort path runs (from step 4's busy check or from a checkpoint) is recorded and
changes nothing; the abort finishes and `create` exits non-zero. SIGKILL remains the way
to stop `create` at once, and the recovery below covers it. A SIGKILL during the abort
path's wait leaves no record, but it does leave the pending abort report from step 2,
which is finished as described under "The abort path".

**Why this cannot deadlock.** The only code that takes the sleeper lock in `create` is
its main line: steps 1, 2, 3.2–3.4, 5's marker write, 6, the K1/K2 deletion and the
abort path's step 2. Each of those takes the lock, does a bounded write (and in step 3
one `SIGSTOP`), and releases it, and none calls another of them while holding it; the
checkpoints that start the deletion or the abort path all sit outside the lock. The
handler takes nothing. So `create` never waits on a lock it holds itself, and its only
waits on the lock are on other processes, which hold it for a bounded write each.

**SIGKILL** (or a crash, or the OOM killer) runs no handler. It leaves a record that the
wake path can recognise and that nothing else produces: **an adopted record in state
`sleeping` at generation 0 whose runner (the `create` process) is dead.** Generation 0
in `sleeping` exists only during the handover: step 6 sets `asleep`, and every later
runner has generation 1 or more. The record shows how far `create` got:

- *No `adopted.frozen_monitor`:* killed before the freeze. Nothing was stopped.
- *`adopted.frozen_monitor` but no `adopted.ending`:* the Monitor may be stopped, so **the
  live session may hear nothing**, and the session has not been signalled.
- *`adopted.ending`:* step 5 had begun. The session has been sent SIGTERM, or is about to
  be, and may already be gone.

A **recovery** finishes what `create` started. It branches on the phase marker:

- **No phase marker, and the session in `adopted.from_pid` alive:** the session was
  never signalled, so the adoption is undone. If `adopted.frozen_monitor` is absent,
  nothing was stopped either: the recovery deletes the record under the lock and
  reports nothing, because no mail can have missed the session. Otherwise it runs the
  abort path: it
  resumes the recorded Monitor if its pid and start time still match (a `SIGCONT` to a
  Monitor that was never stopped changes nothing), deletes the record, waits `W`, and
  reports, with the reason "adoption interrupted: create was killed before ending the
  session". The send that triggered the recovery, if one did, was appended after `F`, so
  it comes out in the burst and is covered by the listing.
- **The phase marker is present:** the session is ending, and the record is **not**
  deleted. If `from_pid` is still alive (same start time), the recovery cannot tell
  whether `create` sent its SIGTERM before it died, so it **always sends SIGTERM itself**
  and allows the full 15 s grace from then, never from the marker's time: SIGTERM is the
  measured clean exit (Test 8), and SIGKILL's effect on the transcript was never
  measured. If the marker is older than 15 s plus the escalation (so `create` died well
  before it could have finished signalling, and a person may have used the pane since),
  the recovery first re-runs step 4's idle and background-work checks, requiring no
  transcript write since the marker's time; whether it should, and what a busy result
  does, is A15 (iv). Then, if the session is still alive 15 s after the recovery's
  SIGTERM, it sends SIGKILL. **Whether or not `from_pid` was alive**, it then sends
  SIGKILL to every descendant in the marker still running with an unchanged start time,
  as step 5 does, and to `frozen_monitor` if its pid and start time still match, and
  polls until they are gone. This covers the likeliest way into this branch: `create`
  sent SIGTERM, was killed during its 15 s wait, and the session exited cleanly, leaving
  the stopped `tail` (own process group, #88) holding the inbox open. It then does step 6: under the lock, computes `read_through` against the now-complete transcript and
  `S`, rewrites the row as a sleeper row, sets `asleep`, and wakes the sleeper through
  the ordinary wake path if any line after `read_through` is unread. Nothing is listed,
  because nothing aborted: the replay covers every line after `S`.
- **No phase marker, and the session already gone** (it exited on its own, or a person
  ended it, after `create` was killed): nothing `create` started is left to undo, and the
  conversation is complete. That is shape A. Recommended (A15): commit, as in the branch
  above, so the agent ends up a sleeper rather than neither live nor a sleeper. There is
  no marker listing descendants here, but `adopted.frozen_monitor` may name a Monitor
  that is still stopped: a stopped `tail` holds a pending SIGTERM without acting on it,
  and it runs in its own process group (#88), so it can outlive the session with the
  inbox open and trip step 1's check (a) on any later adoption of this id. So before it
  commits, if `frozen_monitor` is present and its pid and start time still match, the
  recovery sends it SIGKILL (which ends a stopped process) and polls until it is gone. The
  alternative is to treat it as an abort and delete the record, which leaves a stale row
  whose mail queues with nothing to wake it.

**The recovery never runs inside an inbox writer.** It waits (`W`, and up to 15 s for
the session to exit), and the wake path runs inside every sender: an MCP `send_message`,
`spanreed send`, a Codex worker's reply, and a `conjoin` bridge's frame loop
(`bridge.py`, through `append_message`), all of which call `_wake_after_append`. Running
the wait there would stall that sender, and every other sender to this id behind the
sleeper lock. So the wake path, on recognising the state above, does one of two things
and returns at once (A15 is the choice):

- **(a) Launch the recovery, detached** (recommended): under the sleeper lock the wake
  path already holds, it starts `spanreed sleeper adopt --recover NAME` in a new
  detached tmux session, as a runner is launched, and records the recovery as the
  record's `runner_pid` (with its start time). A later send then sees `sleeping` with a
  live runner and does nothing, which is the rule that already excludes a second wake.
  The wake path launches a recovery for a record at most once (`adopted.recovery_launched`,
  written before the launch). If that recovery dies without finishing, the next wake
  falls back to (b).
- **(b) Report only:** record a wake failure and set `needs_input`, as the orphan rule
  does today, with the reason "adoption interrupted: create was killed; run `spanreed
  sleeper adopt --recover NAME`". Nothing changes until a person runs it.

Either way, `spanreed sleeper adopt --recover NAME` can be run by hand. It runs in the
foreground and prints what it does, and refuses while another recovery for the record
is alive.

**If the recovery is interrupted.** A recovery, hand-run or detached, installs the same
record-only handler as `create`, for SIGINT, SIGTERM and SIGHUP, before it reads the
record, so it too never acts on a signal while it holds the lock. It has one checkpoint,
**R1**, after it reads the record and before it changes anything (no lock held): a flag
found there ends it with the signal's status and leaves the record as it was, for the
next recovery (by hand, or a wake falling back to (b)). After R1 it has started to act,
and every branch runs to the end with the flag recorded and changing nothing, then exits
with the signal's status: the abort branch because its wait is the account the abort
promises, and the commit branches because, as with `create` after K5, the session is
being or has been ended. A SIGHUP (a killed tmux session, or a closed terminal) means its
output may have nowhere to go; it ignores errors writing to its terminal and relies on the
activity log and the report. A SIGKILL is covered as for `create`: before step 2 of the
abort path the record is still there for the next recovery, and after it the pending
report is. `sleeper list` shows an interrupted adoption as such, with its phase (not yet
frozen, frozen, or ending), whether a recovery is running, and how to start one. Until a
recovery runs, a session in the frozen phase hears nothing.

The orphan rule as built (`_wake`, `sleeper_runner.py`) does not apply to this state:
it records a wake failure, sets `needs_input` and launches nothing. That is right for a
runner that died beside its Claude, and is option (b) above, but on its own it would
leave a frozen session deaf until a person read the failure.

Self-adoption (B) remains possible later as "C, run detached once the current turn
ends", if the owner wants agents to put themselves to sleep (A1).

#### Where `read_through` starts

A plain `create` starts at the end of the inbox. An adopted record starts from two
positions:

- **`S`, the inbox snapshot** taken at step 1 of C (for shape A, at validation). It
  bounds what can be history: nothing that arrived after `S` is ever classed as history,
  however the handover goes.
- **The high-water mark (HWM)**: the **last inbox line at or before `S` that the
  adopted session's transcript shows as delivered**, using the existing read-check (the
  three delivered record kinds, each followed by a real turn).

`read_through` is the HWM. Then:

- **Lines after the HWM** follow the ordinary `first_unread_line()` rule: the first wake
  replays every one without a delivered record, and the replay filter skips the ones
  that have one. That includes every line after `S`, delivered by the live session or
  not. For example, a line that arrives during the handover and is lost to the
  Monitor's batch cut, followed by one that is delivered, is still replayed: it is after
  `S`, so the later delivery cannot pull the HWM past it.
- **Undelivered lines at or before the HWM are history, not replayed** (A5). These are
  mail the live session missed while it was awake, before the handover began. The usual
  cause is the Monitor's ~3 KB batch cut (findings Test 9), which affects awake agents
  today. Replaying them would also bring back mail from earlier conversations under the
  same id. `create` lists them by line and `msg_id` so they can be forwarded by hand
  (rule 7: visible).
- **No delivery at or before `S`** in a readable transcript (a conversation that has had
  no mail, or one just after `/clear`, which starts a new session id). The HWM would
  then be line 0 and would replay the inbox's whole history. Recommended instead (A4):
  start after the last line, at or before `S`, that was **stamped on this host** and
  whose `ts` is earlier than the transcript's first record. Only local lines are
  compared by clock. A line relayed from a peer (its `from_agent` is qualified
  `name@host`) carries the sending machine's `ts` (`bridge.py` copies it), so its
  clock is never compared; it is placed by its position in the file, which is the order
  it arrived here. Mail older than the conversation is history, and mail that arrived
  during it without being delivered is replayed. A local line's `ts` is taken just
  before it is appended, on the same clock as the transcript, so only a clock step on
  this machine can misplace it.
- **Compaction** can cut the last delivered records out of the chain, exactly as in
  #88. The HWM then falls back to an earlier line, and the lines between are replayed
  once. That is the safe direction.
- **A rewritten inbox** is handled as it is now: `scan_inbox` detects it and starts over
  from line 1, which can cause duplicates but no loss.
- **When it is computed.** A provisional value is computed at step 2 and stored. The
  final value is computed at step 6, against the complete transcript and the stored
  `S`, and replaces it. Both are at or before `S`.
  If the transcript is unreadable at step 6, the provisional value stays and the existing
  fail-safe applies: `needs_input` is set with the reason, and the backlog after it is
  replayed. A transcript that is unreadable *at validation* refuses the adoption.

#### The registry row, and what stays put

At step 6 the live session's row is **replaced** by the sleeper row: `pid` 0,
`pid_start` −1, the same `agent_id` and `working_dir`, and the display name from the
record (the same name, under `--adopt`). Focus is carried over, because it describes
the conversation that goes on, and status is cleared, as on any re-registration (A12).
The inbox file, every address, and every peer's mirror of the id are unchanged. Peers see
the same id with an `asleep` marker at the next registry sync. `cursors/` is not touched.

**Two hazards that adoption makes likely.** Both exist for any sleeper today, but they
are hard to reach when the sleeper's id is `agent-<name>`:

- **A new session in the same directory takes the sleeper's id.** All three pilots have
  ids derived from their cwd. Anyone who runs `claude` in `~/git/hauler` afterwards
  mints `agent-eac78acd`, and the SessionStart hook's upsert (#16) puts that session's
  pid on the sleeper's row. Its Monitor then tails the sleeper's inbox, mail wakes the
  sleeper as well, and two conversations answer the same message. Recommended (A8):
  `session-start` and `inbox-watch` in a session without `SPANREED_SLEEPER` refuse an id
  that a local sleeper record holds. The session stays off the bus, and its context says
  why and how to join under another name (`SPANREED_AGENT_NAME`).
- **A hand-run `claude --resume <id>`** of the adopted conversation, while the sleeper
  sleeps, gives two Claude processes on one conversation once mail wakes the sleeper.
  Owners are used to resuming these agents by hand. Recommended (A9): before every
  launch, the wake also refuses when any live pid's `~/.claude/sessions/<pid>.json`
  names the record's session id, with a matching `procStart`. This applies to every
  sleeper, works on a best-effort basis (the file is internal), and is never the only
  guard. Adoption runs the same check at step 1 of C, along with the inbox-listener
  check, so a second session that is **already running** when the agent is adopted is
  refused there; A8 covers only sessions that start later.

#### What changes for the adopted agent

- **Its environment becomes the sleeper allowlist** ("Environment"). Anything its
  shell gave it is gone, and in particular `SSH_AUTH_SOCK`. A repo agent that pushed
  over ssh can no longer push from inside the sleeper (A10).
- **Its permission mode is the one given at create**, on every resume, whatever the
  conversation ran with before.
- **Its old pane** is left at a shell prompt once Claude exits. The agent lives on in a
  `spanreed-<name>-<generation>` tmux session, which `sleeper attach` finds.
- **From its first wake on, it sleeps when idle**, like any sleeper.
- **Undoing it:** `retire` the sleeper, then run `claude --resume <session-id>` by hand
  in its cwd. For an id derived from the cwd, the resumed session mints the same id
  again. For an id from `SPANREED_AGENT_NAME`, set the variable first.

#### What adopting grants

Nothing a plain `create` doesn't already grant. The creator still chooses the permission
mode, the claude-args allowlist still applies, and `--trust` still refuses the home
folder, all as in a plain create. Adopting a conversation that ran under
`bypassPermissions` as an `acceptEdits` sleeper narrows its permissions, and the reverse
widens them. Either way that is the creator's choice, as for any create. Reading a
transcript and claiming a bus id need nothing more than running as the user, which can
already `register --agent-id` any id. The ids and names that are already held are
protected only from accidents, by the #80 refusals above.

**`--end-session` is new, though.** No command today ends another agent's live
session; a sleeper is only ever ended by its own runner. With `--end-session`, any
process running as the user, the coordinator included, can end any local agent that
looks idle. Spanreed cannot tell who is running a CLI command, so it cannot enforce a
rule about who may (decision 7: the checks guard against accidents, not agents). Who
should is A14. Whatever the answer, `create` records the caller's identity, as
`_self_identity` resolves it, in the record's `adopted` field and in the activity log,
so an ending is never silent. That identity is asserted, not proven, like every
`from_agent`.

#### Compatibility

- **Existing records need no migration.** Their `agent_id` is `agent-<name>`, so lookups
  by the field give today's answers. The new record fields (`adopted`, below) are
  optional, and records without them read as not adopted.
- **An older spanreed on the same host breaks an adopted sleeper.** It derives the id
  from the name. As a sender it does not wake the sleeper, because its `sleeper_for`
  looks for `eac78acd.json`. As a runner it launches with only `SPANREED_AGENT_NAME`, so
  the woken session registers as `agent-hauler`. Its `prune` drops the pid-0 row, which
  a newer `list` rebuilds from the record. Every spanreed on the host must be upgraded
  before anything is adopted. This is stronger than the existing rule, which only costs
  sleepers their listing.
- **An older binary rewriting the record** (an older runner) drops `adopted`. Once the
  handover is over that field is informational, so nothing breaks. During the handover
  no runner rewrites the record, because `create` is its runner; but an older spanreed
  would not recognise an interrupted adoption either, which is one more reason every
  spanreed on the host must be upgraded first.
- **Peers** see an unchanged row format.

The record gains one optional field: `adopted`, which holds `at`, `by` (the caller's
asserted identity), `from_pid`, `from_pid_start`, `inbox_snapshot` (`S`), and
`read_through_basis` (`transcript`, `conversation_start` or `provisional`). It records
where the agent came from, who adopted it, and why it starts reading where it does.
While `create` runs, it also holds the handover's phase, which is what lets a recovery
finish a `create` that was killed ("If `create` is interrupted"):
- `frozen_monitor` (`pid`, `pid_start`, and the freeze mark `F`), written under the lock
  **before** the Monitor is sent `SIGSTOP` (step 3);
- `ending` (`at`, and `descendants`, each a `pid` and `pid_start`), the phase marker,
  written under the lock **before** the session is sent its first SIGTERM (step 5);
- `recovery_launched`, set by the wake path before it launches a detached recovery
  (A15 (a)).

`sleeper list` shows it. An aborted adoption leaves no record; its listing is in
`sleepers/<name>.adopt-aborted.json` ("The abort path"), which `sleeper list` also shows.

#### Not verified yet (adoption)

Nothing here has been built or run. To measure in the implementation PR:
- **`claude --resume <id>` on a conversation last run interactively**, without
  `--session-id`, in the owner's own tmux and not spanreed's, and compacted many times.
  Test 6 measured only a conversation that spanreed started with `--session-id`.
- **Resume with a different `--permission-mode`** from the one the conversation last
  ran with. Test 6 passed the same mode again.
- **Whether `--resume <id>` needs the original cwd.** The `cwd` check assumes it does.
- **SIGTERM to a session mid-turn**, which A2 exists to avoid. Test 8 measured idle
  only.
- **`~/.claude/sessions/<pid>.json`** beyond what findings.md records: whether it is
  removed at exit, and what each `status` value means.
- **Whether `sessions/<pid>.json` updates its `sessionId` after `/clear`.** A4 assumes
  `/clear` starts a new session id. If the file keeps the old one, `--adopt` would
  resume the conversation from before the `/clear`, not the one the agent is in. The
  implementation PR must check this before `--adopt` trusts the field, or compare it
  with the newest transcript in the cwd's project folder.
- **Whether `cwd` in `sessions/<pid>.json` is the start directory** (the one the id was
  minted from) or follows the session. Step 1's check (c) assumes the start directory.
- **The idle checks of A2**: whether an idle session ever writes to its transcript
  without a turn, whether a turn in progress always shows as a final record with no
  answer (a `tool_use` with no `tool_result`, or a user record with no assistant
  reply), and whether a foreground tool call writes nothing while it runs.
- **The descendant-process backstop of A13**: which descendants a session has when idle
  (MCP servers, the Monitor, anything else), so that the rest can be read as work.
- **The abort path's wait `W`**: how long a burst's turn takes to show as delivered.
- **`SIGSTOP` on the Monitor's `tail`**: that Claude Code does not react to a stopped
  Monitor. The reviewer of #129 measured the `tail` side (findings.md): the wrapper
  does not exit, and `SIGCONT` emits every line appended meanwhile at once. How Claude
  Code turns that burst into events is assumed to be Test 9's batching and cut, and has
  not been measured on a resumed Monitor.
- **The background-work pairing of A13**: whether every kind of background work (a
  background Bash, a background agent, work a subagent launches, a `Monitor` other than
  the inbox's) writes a launch record and a `<task-notification>` with a matching
  `<tool-use-id>`, whether a launch that dies with its process ever gets one, and how
  to read the process start time against record timestamps. Measured so far on one
  coordinator transcript, and the notification statuses across every project.
- **Monitors in the pairing.** The reviewer of #129 found that 19 of the 21 Monitor-tool
  notifications on one transcript carry no `<tool-use-id>`, so the design leaves Monitors
  out of the pairing and relies on the descendant backstop for them. That rests on a
  running Monitor always being a descendant process of the session, and on a finished
  one leaving none; neither has been checked.
- **Background work across `/clear`.** Whether a background agent launched before a
  `/clear` keeps running after it, and where its `<task-notification>` is written (the
  old transcript, the new one, or neither). The pairing reads every transcript written
  since the process started, so wherever it lands the launch is seen; a notification
  that lands nowhere leaves the launch unpaired, which refuses the adoption until the
  session restarts. Also whether a transcript's last-write time can be trusted to be at
  or after every record in it, which the "written since `procStart`" filter assumes.
- **The interrupted-adoption recovery** (A15): that a detached recovery launched from the
  wake path starts promptly and finishes within about `W` plus 15 s plus the escalation.
- **`CLAUDE_CODE_SESSION_ID`** beyond the one observation in findings.md, which
  self-adoption (B) would read.
- **What `claude --resume` does when the conversation is already open in another
  process.** `claude --help` (2.1.283) says only that `--bg --resume` "starts a copy and
  says so when the session is already running".
- **Resuming a transcript as large as a pilot's**, in launch time and memory. That is
  the RAM this feature exists to free.

### Commands

```
spanreed sleeper create --name X --cwd DIR --permission-mode MODE [--model M] [--idle-timeout 15m] [--trust] [-- CLAUDE_ARGS...]
spanreed sleeper create --adopt AGENT [--end-session] --permission-mode MODE [--name X] [...]   # designed, not built
spanreed sleeper create --name X --cwd DIR --session-id UUID [--agent-id ID] --permission-mode MODE [...]   # designed, not built
spanreed sleeper adopt --recover X   # finish a killed adoption; designed, not built
spanreed sleeper adopt --dismiss X   # clear an aborted adoption's report; designed, not built
spanreed sleeper list
spanreed sleeper attach X     # tmux attach to the current session-name generation
spanreed sleeper wake X       # by hand; the same path a message takes
spanreed sleeper sleep X      # by hand; the same as request_sleep
spanreed sleeper retire X     # exits it if awake, deletes the record, removes the row
```

With `--host PEER`, `create`, `list`, `wake`, `sleep` and `retire` run on a
conjoined peer instead (designed, not built yet; see "Managing a peer's sleepers over
`conjoin`"). `attach --host` prints the command to run on the peer.

`create` refuses a name already held by a live agent or an existing sleeper (the
same rule as #80). "Held" covers a present agent's **display name** as well as its
id, since a send by name would otherwise be ambiguous between them. `retire` leaves
the inbox file in place as history. Mail addressed to a retired sleeper is refused,
like mail to any unknown agent.

Settled while building stage 1 (the author's calls, listed for the owner to see):
- **`create` launches nothing.** The sleeper starts `asleep`; the first mail, or
  `sleeper wake`, is its first launch (with `--session-id`).
- **`--permission-mode` is required**, and must be one `claude --help` lists
  (checked at create, since a typo would otherwise fail every wake). Leaving it
  optional was considered and dropped in the review of PR #89: with no flag,
  each launch takes whatever Claude Code's settings say at that moment, so the
  mode would not be "kept" (decision 2), and resume without the flag was never
  measured.
- **An asleep sleeper's row carries `pid` 0 and `pid_start` -1**, which no
  process has (see "Compatibility").
- **`retire` refuses an awake or sleeping sleeper** until stage 3 can end one.
- **Extra `claude` arguments, after `--`, are fixed at create and passed verbatim
  on every launch**, after spanreed's own: the first `--session-id` launch and
  each `--resume`. This is the owner's feature request (2026-11-17): a tool
  managing sleepers gives each its own MCP servers or system-prompt text
  (`--mcp-config`, `--append-system-prompt-file`) without writing files into the
  sleeper's directory, which may be someone's git worktree. The workaround it
  replaces, a wrapper named `claude` first on PATH, left the wrapper's folder on
  the sleeper's PATH for everything it ran. **Only an allowlist of flags is
  accepted** (the owner's decision, 2026-11-17, after the review of PR #100
  showed a denylist let `--allowedTools`, `--add-dir`, `--settings` and
  `--allow-dangerously-skip-permissions` through): `--mcp-config`,
  `--strict-mcp-config`, `--append-system-prompt[-file]`, `--system-prompt`,
  `--disallowed-tools`, `--effort`, `--fallback-model`, `--verbose`,
  `--debug-file`, `--name`. Nothing on it can widen a sleeper's permissions past
  the mode fixed at create (decision 2), change which conversation it resumes,
  or make it non-interactive; a bare word, which `claude` would read as a first
  prompt, is refused too. A flag Claude Code adds later is refused until it is
  added deliberately. **Arguments are not rewritten** (the author's call): a
  relative path resolves against `--cwd`, so absolute paths are the safe
  choice. They are stored in
  the record as `claude_args`, shown by `create` and `sleeper list`.
- **`--model` is fixed at create and passed on every launch**, like the permission
  mode. Without it a sleeper gets the model in Claude Code's settings, which need
  not be the one the owner's own sessions run (they pass `--model`).

**A folder Claude Code has never trusted: `create --trust`, and warnings.** The
first launch in such a directory stops at Claude Code's "do you trust this
folder?" dialog (measured, findings Test 12). A woken sleeper sitting at that
dialog never takes a turn and never sleeps. *The owner's decision* (2026-11-14,
asked "Could we have a flag that approves?", then "Sure" to this design):

- **`create --trust`** marks the folder trusted the way accepting the dialog
  does: it sets `projects.<cwd>.hasTrustDialogAccepted` in Claude Code's state
  file (`~/.claude.json`, or `$CLAUDE_CONFIG_DIR/.claude.json`) and reads it back.
  It runs before the record is written, so a failure creates nothing. Claude Code
  has no flag of its own for this; its dialog is skipped only in `-p` mode.
- **The home folder and anything above it are refused.** Trust is inherited, so
  a grant there would cover nearly everything, and Claude Code itself never saves
  home-folder trust (review of PR #93). Use a project folder.
- **Running sessions rewrite that file, and nothing locks it against them.** The
  write sets the one key on a fresh read and replaces the file only if its inode,
  size, mtime and ctime are unchanged since, retrying up to 5 times. That narrows
  the window without closing it: a session write landing between the last check
  and the rename is lost (the review of PR #93 measured 2 of 3,383 against a
  1 kHz writer; the file was never damaged).
- **A symlinked state file is written through**, next to its real target, so a
  dotfile manager's link stays a link. Non-ASCII is kept as-is, so unrelated
  bytes round-trip unchanged.
- **Without `--trust`**, `create` and `sleeper list` warn when the folder is not
  trusted, and the runner sets `needs_input` at a launch into one, since nobody
  sees a detached pane. Trust is inherited from parent folders **up to the
  enclosing git repository's root, not past it** (measured, findings Test 14),
  and the check follows exactly that.
- **The file is Claude Code's internal format**, so this fails toward "not
  trusted". An unreadable file reads as unknown, with its own warning, and the
  worst case of any failure is the dialog, exactly as without `--trust`.
- Driving the dialog itself from the runner was considered and declined: it
  depends on the dialog's wording and choice order, which any release can change.

### Compatibility

The `asleep`/`awake`/`sleeping` marker on a registry row is a **new field** with a default, and peers mirror it as-is,
not a new `status` value; an older spanreed rejects any `status` it doesn't
recognize when it reads the registry. `Agent` is a default pydantic model, so an
older binary ignores the new field when reading.

Older binaries still hurt sleepers in two ways:
- An older binary decides liveness by pid, so sleepers disappear from its `list`
  while asleep, and it treats mail to one as queued for a stopped session. That
  depends on the row's `pid_start` being **`-1`**, which no process has. An older
  binary reads pid 0 as alive (`kill(0, 0)` signals its own process group), and
  with `pid_start: null` it would trust that: it would list an asleep sleeper as
  LIVE and tell senders "Delivered", the #55 failure (measured in the review of
  PR #89). With `-1` it sees a start time that does not match and reads the row
  as stale.
- **An older binary that rewrites the registry on the same host** (`set_status`,
  `prune`) **drops the marker or prunes the row.**

The sleeper record is unaffected, so the row is restored at the next wake or
`list`. But every host needs the new version.

### Not verified yet

Measured in Test 6: starting with `--session-id`, `/exit` and its confirmation
dialog, resume restoring the conversation, resume keeping the permission mode when
it is passed again, and the Monitor restarting on resume. Everything below is
assumed until measured, and the implementation PR has to measure it:
- ~~`UserPromptSubmit` firing for turns the inbox Monitor starts~~: **measured,
  it does** (Test 7). So do `Stop` with the Monitor running, and an `idle_prompt`
  notification 60 s after `Stop`.
- **A `Notification` of type `permission_prompt` reaching a plugin hook.**
- ~~Waking on mail, end to end~~: **measured, it works** (findings Test 12). A
  message woke an asleep sleeper, the Monitor replayed it, Claude answered; a
  SIGTERM put it to sleep with `read_through` moved past it; two messages sent
  while asleep woke it again with `--resume`, arrived as two separate events at
  the 3 s replay pace, and Claude still had the earlier conversation.
- **SessionStart `source: "resume"` re-registering the row.** The `resume` matcher
  exists in `hooks.json`, but Test 6 didn't check the registry after the resume.
- **`tmux new-session -d` from an ssh-launched serve end outliving the ssh
  session:** **measured, it does** on kaladin and adolin (findings Test 10), where
  `KillUserProcesses` is off. But a tmux **server** that an ssh-launched wake
  starts stays in the ssh login's scope, so on a host with
  `KillUserProcesses=yes` it would die with the session. **Open, the owner's
  call:** start tmux via `systemd-run --user --scope` to rule that out
  everywhere.
- **A failed wake from a fresh launch.** Measured in the review of PR #84: a
  session that stays up through an expired login or a rate limit answers every
  turn with synthetic error records. Not measured: what a *fresh* `claude
  --resume` does when the login is already expired. It may exit, or show a login
  prompt; the runner has to handle both.
- ~~A session ended by SIGTERM or SIGKILL resuming cleanly~~: **measured, it
  does** (findings Test 8), at idle. Still open: a signal sent **mid-turn or
  mid-write**, or with an `enqueue` pending.
- ~~The transcript being complete after a signal~~: **measured complete** after
  SIGINT, SIGTERM and SIGKILL at idle (Test 8). The negative control the
  implementation needs is unchanged: a transcript holding only the `enqueue` for M
  must report M unread.
- ~~A replaying Monitor reader~~: **measured, it delivers** (findings Test 9).
  Lines that arrive close together are batched, and a batch is cut off at ~3 KB;
  see "Waking". Still open: how wide the batching window is, so the replay pacing
  can be chosen.
- ~~Background work other than the Monitor~~: **measured** (Test 11). The `Stop`
  hook's `background_tasks` lists it; task hooks don't fire. ~~In a resumed
  session~~: **measured, it does too** (Test 13), and it kept a sleeper awake past
  its idle timeout until `sleeper sleep` stopped it.
- ~~`Stop` and `UserPromptSubmit` firing in a resumed session~~: **measured, they
  do** (Test 13).
- **A Monitor event queued behind the final turn** starting its own turn within the
  quiet period after `request_sleep`. If it doesn't, step 4 still catches the
  message; this only decides whether a sleep is skipped or happens.
- ~~How a resume behaves when mail arrived while the session was down~~:
  **measured** (Test 9). With `tail -n 0` it's never delivered; started mid-file,
  it is.
- **The `/exit` confirmation dialog** keeping its current wording and default across
  Claude Code versions. The runner must check that Claude actually exited, not trust
  the keys it sent.
- **The tmux session teardown window:** measured in the review of PR #84, which is
  why session names include `generation`. Not yet measured with that fix in place.
- **An expired login makes every wake on that machine fail at once.** Every
  sleeper shares the one Claude Code login, and mail piles up behind all of them.
  The design cannot fix this, since logging in needs a human. It can make it loud
  and early:
  - each failed wake records the reason and sets `needs_input`;
  - `sleeper list` groups wake failures by reason, so "12 sleepers, one cause" reads
    as one problem;
  - while a sleeper is awake, the runner watches for Claude Code's "login expires in
    N day(s)" warning and reports it before it starts failing wakes. That warning
    is visible on screen (Test 6), but whether a hook or a file exposes it isn't
    known yet.
- **macOS**, where nothing here has been run. One point is not an open question:
  the lock is `fcntl.flock`, the system call. macOS has it, and spanreed's registry
  lock already depends on it there. The `flock` **command-line tool** isn't on
  macOS, and nothing here uses it.


- **Typing into a resumed session.** In Test 8 the first prompt sent with
  `send-keys … Enter` to each resumed session was left unsubmitted. The design no
  longer types prompts on wake, since the Monitor delivers. But anything that does
  type must check the input was actually submitted.

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

Link liveness reuses the agent liveness model exactly (bridge PID alive + start-time match), so a link's state can never disagree with the state of the mirrored entries that bridge owns. A link is in one of three states: **attached** (the bridge process runs and its link is up), **reconnecting** (a `conjoin` runs but its link dropped and it is retrying; `detached_at` is set and the pid is alive), and **stopped** (no bridge process for that host runs, or its `conjoin` has begun a clean exit: `stopping_at` is written before the exit clears any mirrored row, so a pid that lives on for the last few milliseconds never reads as reconnecting). A serve end is never reconnecting: its process ends with its link. Records are **kept** after teardown with `detached_at` set: "a bridge was here and died" is a diagnosis, and deleting the file would make it read as "no bridge was ever configured".

### The core trick: reuse inboxes as the outbound queue

The bridge **mirrors the peer's live agents into the local registry**, qualified by host (`agent-X@hostB`) and owned by the bridge's own PID. That PID is the bridge **process**, not one connection: on the connect end it is `conjoin` itself, whose reconnect loop outlives every dropped link. Both self-set presence fields cross the bridge: a mirrored entry carries the remote agent's `focus` *and* its `status`, so the "who needs a human" scan over `list_agents` (`status ∈ {needs_input, blocked}`) sees remote agents exactly as it sees local ones. Everything else falls out of the existing primitives with no MCP changes:

- A local agent sends to `agent-X@hostB` → ordinary `send_message` → lands in `inboxes/agent-X@hostB.jsonl` locally.
- The bridge tails every `*@hostB` inbox and forwards new lines over the pipe.
- On hostB, the bridge appends the message to the *real* local `inboxes/agent-X.jsonl`. hostB's agent-X monitor (`tail -F`) fires exactly as for a local message.

Replies are symmetric: agent-X replies to `agent-Y@hostA`, which lands in hostB's `inboxes/agent-Y@hostA.jsonl`, which hostB's bridge tails and forwards back. Purely-local traffic never touches the bridge (it lands in bare inboxes, not `*@peer` ones).

### Identity rewriting

Global identity is `agent-X@homehost`; on its home host the agent is the bare `agent-X`, on a foreign host it's `agent-X@home`. The **sending** bridge rewrites addresses into the *receiver's* namespace before putting a frame on the pipe (`to` = bare local id on the receiver; `from` = qualified with the sender's host), so the receiving bridge just appends. See `protocol.md` for the exact rules.

### Properties that fall out for free

- **No cross-host heartbeat.** Remote-agent liveness is just "present in the peer's latest registry snapshot," which the peer computes with the local PID + start-time check. The snapshot's *age* is recorded on the peer record, so a caller can see how old that evidence is rather than assuming it is current. The bridge process's PID backs the mirrored entries. On the connect end that is `conjoin`, so the remote agents stay in `list_agents` while it reconnects and vanish when it exits or dies; on the serve end, whose process ends with its link, they vanish with the link.
- **Store-and-forward across disconnects, while `conjoin` runs.** The rule, decided by the owner on 2026-11-25 ([#34](https://github.com/Monkopedia/spanreed/issues/34)), turns on whether a `conjoin` process for that host is running, not on whether its link is up:
  - **`conjoin` running, link down** (its reconnect loop is retrying): **queue.** The peer's mirrored agents stay addressable for the life of that `conjoin` process, sends go into the outbound `*@peer` inbox, and the reconnect drains them: the bridge advances its saved cursor (the existing `cursors/` mechanism) only after a confirmed send, so it resumes from that cursor. The delivery verdict says the message is queued because the bridge is reconnecting, and when it last connected. A dropped link does not clear the mirrored rows; `conjoin` clears every row it owns when it exits.
  - **No `conjoin` running for that host** (stopped, crashed or killed): **refuse**, saying no bridge is running to deliver it. A killed `conjoin` never runs its exit, so its rows linger in the registry with a dead owner pid; the resolver refuses an exact `agent-X@peer` address whose row's owner is dead, or whose host's peer record already reads stopped and names that owner (a clean exit marks it just before clearing the rows; a row another live bridge mirrored, such as the `serve` end of a two-way pair sharing the record, still resolves), rather than queuing into an inbox nothing forwards until a `conjoin` to that host is started again ([protocol.md, "Recipient resolution"](protocol.md#recipient-resolution)). That check narrows the stopping window but cannot close it: the sender and `conjoin` are separate processes and no lock spans both, so a `conjoin` that stops between resolution and the append lets the send through. That send is accepted, its verdict says no bridge is running once the stop has marked the record, and the next `conjoin` to that host forwards it ([protocol.md, "Delivery is not resolution"](protocol.md#delivery-is-not-resolution)).

### Launch and prerequisites (empirically settled)

- `spanreed` must be installed on each bridged host.
- The remote `spanreed` is invoked by **absolute path**, because a non-interactive SSH command gets a stripped `$PATH` (`~/.local/bin` is typically added in `.zshrc`, which login/non-interactive shells don't source). The `connect` side discovers the path once via an interactive-shell probe — `ssh host 'zsh -ic "command -v spanreed"'` returns the bare path cleanly — then launches `serve` by that path.
- **Key-based (non-interactive) auth is required**: the bridge re-establishes itself without a human present, so it can't answer a password prompt.

### Reconnect

`spanreed conjoin <host>` is a long-lived **foreground** command. When the pipe drops it re-establishes it with exponential backoff + jitter (a connection that stays up long enough resets the backoff). A dead pipe is detected three ways: EOF on the SSH child, SSH keepalive (`ServerAliveInterval`), and a receive-side watchdog (no frame — not even the peer's pings — within `recv_timeout`). A dropped link leaves the mirrored `@peer` entries in place, so they stay addressable and mail for them queues while `conjoin` reconnects ([#34](https://github.com/Monkopedia/spanreed/issues/34)). When `conjoin` itself ends (SIGINT/SIGTERM, or `--max-reconnects` giving up) it clears every mirrored entry it owns and kills the SSH child. The serve end clears its entries at teardown, since its process ends with its link.

Delivery across a drop is **at-least-once**: the outbound cursor advances only after a confirmed send (nothing lost), and delivery dedupes by `msg_id` (nothing double-delivered on resend). **Supervision is deliberately out of scope** — `conjoin` restarts the *pipe*, not itself. If you want it to survive reboots or crashes, wrap it in systemd/launchd/tmux.

### Scope (v1)

Point-to-point: two machines, one direct bridge, launched manually per pair. Transitive/multi-hop routing (B reaching C through A) and auto-discovery of peer hosts are deferred — see `open-questions.md`.
