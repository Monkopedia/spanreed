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

This separation surfaced empirically from test #1: when a signal carried embedded instructions ("reply ACK"), Claude correctly refused — monitor stdout isn't a trusted command channel. The fix isn't to defeat the defense, it's to put policy in the description (trusted) and treat message content as data (not commands).

## Escalation: PushNotification

Discovered in test #3: Claude can call the `PushNotification` tool to alert the human when something needs attention. The harness automatically suppresses the notification when the user is active in the receiving terminal — so worker agents can flag without spamming. The bus policy should bundle this in: when a response status is `needs-user-attention`, also call `PushNotification` so the human gets pulled in even if they're heads-down in a different terminal.

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

The bridge **mirrors the peer's live agents into the local registry**, qualified by host (`agent-X@hostB`) and owned by the bridge's own PID. Everything else falls out of the existing primitives with no MCP changes:

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

### Scope (v1)

Point-to-point: two machines, one direct bridge, launched manually per pair. Transitive/multi-hop routing (B reaching C through A) and auto-discovery of peer hosts are deferred — see `open-questions.md`.
