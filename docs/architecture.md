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

So a forged sender is not a threat this design defends against. The boundary that carries
that trust is the same one stated at `:105` and `:111` — **the single-user assumption: "you
can reach the box"** — and it reaches *further than one machine*, because `spanreed conjoin`
bridges a peer's bus over SSH and the receiving side appends peer frames verbatim
(`bridge.py:113`). A bridged agent is not co-located. `open-questions.md` states the same
boundary for the bridge explicitly. **If that assumption ever stops holding, this row is the
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

### The core trick: reuse inboxes as the outbound queue

The bridge **mirrors the peer's live agents into the local registry**, qualified by host (`agent-X@hostB`) and owned by the bridge's own PID. Both self-set presence fields cross the bridge: a mirrored entry carries the remote agent's `focus` *and* its `status`, so the "who needs a human" scan over `list_agents` (`status ∈ {needs_input, blocked}`) sees remote agents exactly as it sees local ones. Everything else falls out of the existing primitives with no MCP changes:

- A local agent sends to `agent-X@hostB` → ordinary `send_message` → lands in `inboxes/agent-X@hostB.jsonl` locally.
- The bridge tails every `*@hostB` inbox and forwards new lines over the pipe.
- On hostB, the bridge appends the message to the *real* local `inboxes/agent-X.jsonl`. hostB's agent-X monitor (`tail -F`) fires exactly as for a local message.

Replies are symmetric: agent-X replies to `agent-Y@hostA`, which lands in hostB's `inboxes/agent-Y@hostA.jsonl`, which hostB's bridge tails and forwards back. Purely-local traffic never touches the bridge (it lands in bare inboxes, not `*@peer` ones).

### Identity rewriting

Global identity is `agent-X@homehost`; on its home host the agent is the bare `agent-X`, on a foreign host it's `agent-X@home`. The **sending** bridge rewrites addresses into the *receiver's* namespace before putting a frame on the pipe (`to` = bare local id on the receiver; `from` = qualified with the sender's host), so the receiving bridge just appends. See `protocol.md` for the exact rules.

### Properties that fall out for free

- **No cross-host heartbeat.** Remote-agent liveness is just "present in the peer's latest registry snapshot," which the peer computes with the local PID + start-time check. The bridge's own PID backs the mirrored entries, so if the pipe dies the remote agents correctly vanish from `list_agents`.
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
