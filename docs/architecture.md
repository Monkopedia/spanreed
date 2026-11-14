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
`conjoin` (#86) is not built.
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
  This is decision 8, tracked in #86. See "Across `conjoin`".

Also the author's, for the owner to see: no ssh agent in a woken sleeper (see
"Environment"), and a signal-first exit.

### The sleeper record

`<state>/sleepers/<name>.json`, owner-only like the rest of the state root. It is the
source of truth, and it survives a reboot:

| field | meaning |
|---|---|
| `name`, `agent_id` | `agent_id` is `agent-<name>`, validated by `validate_agent_id` |
| `cwd` | the directory Claude runs in |
| `session_id` | a UUID chosen at create; `--session-id` on the first launch, `--resume` after that |
| `permission_mode` | fixed at create; passed on every launch |
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
replay X's whole history into a fresh conversation.

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
4. If the link is down, the mail waits on the sending machine, and the reconnect
   delivers it and wakes the sleeper. That is durable end to end.

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
from here, e.g. `sleeper create --host`) is **wanted, and comes after local sleepers
work end to end.** That is the owner's decision 8 on #85: "Get everything working
first, then let's worry about it, but I would like support for management over
conjoin". It is tracked in #86, along with the questions it raises: the carrier,
authorizing a remote create, and resolving paths on the target host. It is not
part of the first implementation. Mail already wakes a remote sleeper without it.

### Commands

```
spanreed sleeper create --name X --cwd DIR --permission-mode MODE [--model M] [--idle-timeout 15m] [--trust]
spanreed sleeper list
spanreed sleeper attach X     # tmux attach to the current session-name generation
spanreed sleeper wake X       # by hand; the same path a message takes
spanreed sleeper sleep X      # by hand; the same as request_sleep
spanreed sleeper retire X     # exits it if awake, deletes the record, removes the row
```

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
- **Running sessions rewrite that file**, so the write must not undo theirs: it
  sets the one key on a fresh read and replaces the file only if its mtime and
  size are unchanged since, retrying up to 5 times otherwise.
- **Without `--trust`**, `create` and `sleeper list` warn when the folder is not
  trusted, and the runner sets `needs_input` at a launch into one, since nobody
  sees a detached pane. Trust is inherited from parent folders (measured,
  findings Test 14), and the check follows that.
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
