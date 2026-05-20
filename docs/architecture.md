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
- Cross-host messaging (single machine only for now)
- Persistence across machine reboots
- Authentication / authorization between agents (single-user assumption)
