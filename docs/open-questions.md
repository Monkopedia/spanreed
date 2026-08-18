# Open questions

Things still to test, design, or decide.

## Behavioral / empirical

- **`/reload-plugins` doesn't restart already-running MCP servers.** Reloading the plugin picks up changes to the manifest, hooks, monitors, and `.mcp.json` config, but the MCP server process spawned at session start keeps running on the *old* code. Adding a new MCP tool, renaming a tool, or changing tool signatures all require a full Claude session restart to take effect. Existing tools continue to work because the MCP server reads StateStore state fresh on each call — so behavioral changes to *implementations* of existing tools land on next call, but new tool *registrations* don't surface until restart. Development-workflow gotcha; possibly worth `/feedback` asking for `/reload-plugins` to restart MCP server processes too.
- **Monitor exit-warning UX**: a plugin Monitor shows up as "1 monitor still running" in the status line and triggers a "background work in progress" warning on session exit. There's no documented way to mark a monitor as essential infrastructure that should be quietly ignored on exit (no `essential` / `silent` / `quiet` field; no settings override). For an always-on bus, this gets annoying. Options: file `/feedback` asking Anthropic for the flag, or investigate whether MCP server-initiated notifications can replace Monitor as the wake mechanism (avoids the warning entirely, but unclear if Claude Code wakes on them).
- **Multi-message delivery**: if N messages arrive between Claude's turns, do all N surface or do they coalesce / drop?
- **Mid-typing behavior**: what happens to a notification fired while the human is mid-typing a prompt?
- **Stop hook + `additionalContext`**: could a Stop hook surface "you have pending bus messages" at natural turn boundaries as a complement to the Monitor signal? (Also relevant to agent status: a Stop hook could set `status` to `idle` at turn end, making the `idle`/`working` distinction reliable instead of best-effort — see the "Agent status" section in `architecture.md`. Deferred for v1; the human-need levels work without it.)
- **Long-running reliability**: do monitors stay healthy across hours / days of session uptime? Docs say monitors don't auto-restart on crash — how do we recover?
- **Agent Teams as cautionary signal**: [open issues](https://github.com/anthropics/claude-code/issues/23415) show Anthropic's own multi-agent mailbox has unresolved delivery bugs. Do we inherit them? Where does our design diverge?

## Design

- **MCP tool surface**: final shape of `register`, `send`, `recv`. Acknowledgments? Read receipts? Conversation threading?
- **State format**: JSONL append-only? SQLite? File-per-message?
- **Agent identity**: what makes two sessions "the same agent" vs. distinct? Working directory? Manual name? PID?
- **Conversation continuity**: can a message reference a thread, and Claude pick up context from prior messages in it?
- **Self-deregistration on crash**: if a session dies without running the Stop hook, the registry has stale entries. PID-based liveness? TTL? Lease renewal?
- **Where the disposition policy lives** (resolved): the full policy is a single constant (`_DISPOSITION_POLICY` in `cli.py`), embedded once in the SessionStart context and written to `<state_root>/disposition-policy.md`. The inbox Monitor's `description` is a one-liner that points at that file — so the ~400-token policy is no longer re-injected on every monitor event (it was both wasteful and redundant with the SessionStart context). The file is written by **both** `session-start` and `inbox-watch` (the Monitor command): binding its existence to the same mechanism that references it means a live monitor can't point at a missing file regardless of hook ordering or a deleted file. (Note: a box on an old `spanreed` whose plugin/monitor already references the file but whose CLI predates the file-write will still see it missing until the package is upgraded — version-skew, resolves on adoption.) Open sub-question: whether to also slim the SessionStart context to a pointer (currently it still embeds the full policy, since that's a once-per-session cost).
- **Daemon lifecycle**: does the MCP server run per-user as a long-lived daemon (systemd / launchd) or spawn-on-demand from the first plugin connection?
- **Activity-log retention/rotation**: `activity-log.jsonl` is append-only and unbounded. With focus softened to major-task granularity it grows slowly, and `spanreed log --since` bounds *reads* regardless of file size, so rotation is deferred. Revisit if the file gets large (size- or age-based truncation, or a `--prune`).
- **Activity-log cross-host replication**: the log captures local `set_focus`/`set_status` calls only. A conjoined peer's transitions don't appear in the local log (the bridge mirrors remote agents via `sync_remote_agents`, not `set_focus`). Moot for a single host; for a multi-host digest, decide whether the bridge forwards activity records or each host's log is summarized separately.

## Cross-host bridge (in design)

Design lives in [`architecture.md`](architecture.md) (SSH bus-bridge) and [`protocol.md`](protocol.md) (wire-format). Resolved so far:

- **Transport**: persistent duplex SSH pipe between two symmetric bridge processes; no broker, no shared filesystem.
- **Remote launch**: invoke the remote `spanreed` by **absolute path**, discovered once via an interactive-shell probe (`ssh host 'zsh -ic "command -v spanreed"'`), because non-interactive SSH gets a stripped `$PATH`.
- **Auth**: key-based / non-interactive SSH required (the bridge reconnects unattended).
- **Per-host install**: `spanreed` must be installed on every bridged host.
- **Sender authentication** (resolved 2026-08-18): `send_message`'s `from_agent` is caller-asserted and verified nowhere. Asked *"should it be authenticated — derive from the calling process, validate against the registry, or leave as is?"*, the owner answered *"no, if its on the bus you can trust it"*. Accepted design, not a gap. The two alternatives were also measured and rejected on cost: as of 2026-08-07 (at `1626501`) 31% of bus traffic — 1590 of 5089 messages, from 47 senders including cron jobs, the webhook receiver, and triage/reviewer subagents — came from senders with no session to derive an identity from, so deriving from the calling process breaks the fleet's automation layer, and validating against the registry breaks the same senders without preventing impersonation. See `Monkopedia/spanreed#16`. **Scoped to the trust boundary in the bullet below** — it is what the ruling rests on. Does *not* cover an id that is wrong by **accident** (`#23`), nor relayed claims about the owner's intent, which are a question about authority rather than transport.
- **Trust boundary**: SSH access to a host grants full read/write to that host's entire bus (the bridge can inject into any local inbox). Intended under the single-user assumption, but stated explicitly.
- **Reconnect**: `spanreed conjoin` is a foreground command that re-establishes the pipe with exponential backoff + jitter when it drops. Death is detected via EOF, SSH keepalive (`ServerAliveInterval`), and a receive-side watchdog (no frame within `recv_timeout`). SIGINT/SIGTERM tear down cleanly (clear mirrored entries, kill the SSH child). Supervision (start-on-boot, restart-on-crash) is deliberately **out of scope** — wrap it in systemd/launchd/tmux if you want a service.
- **Delivery across a drop**: at-least-once. Outbound advances the `*@peer` cursor only after a confirmed send (no loss); `append_message` dedupes by `msg_id` on delivery (no double-delivery on resend). Validated: a serve killed repeatedly drops then re-mirrors its agents as the bridge respawns.

Still open:

- **Multi-hop / transitive routing** (B reaching C through A): deferred. v1 is point-to-point only.
- **Peer-host discovery**: v1 launches bridges manually per pair. A peer-host config/list is future work.
- **Peer up/down signal to agents**: when a peer connects or drops, should local agents be *notified* (a bus event), or only observe it via `list_agents`? Currently the latter.
- **Registry-sync cadence**: poll interval / change-detection for the `registry` frame; tradeoff between freshness and chatter.
- **`@host` UX in `list_agents`**: how qualified ids and "this peer is via bridge" surface to the user (per the visibility-over-hiding principle).

## Out-of-scope (for now)

- Persistence across machine reboots
- Authentication / authorization between agents (single-user assumption)
- Streaming / long-form message bodies (current model is single-shot JSON messages)
