# Open questions

Things still to test, design, or decide.

## Behavioral / empirical

- **`/reload-plugins` doesn't restart already-running MCP servers.** Reloading the plugin picks up changes to the manifest, hooks, monitors, and `.mcp.json` config, but the MCP server process spawned at session start keeps running on the *old* code. Adding a new MCP tool, renaming a tool, or changing tool signatures all require a full Claude session restart to take effect. Existing tools continue to work because the MCP server reads StateStore state fresh on each call — so behavioral changes to *implementations* of existing tools land on next call, but new tool *registrations* don't surface until restart. Development-workflow gotcha; possibly worth `/feedback` asking for `/reload-plugins` to restart MCP server processes too.
- **Monitor exit-warning UX**: a plugin Monitor shows up as "1 monitor still running" in the status line and triggers a "background work in progress" warning on session exit. There's no documented way to mark a monitor as essential infrastructure that should be quietly ignored on exit (no `essential` / `silent` / `quiet` field; no settings override). For an always-on bus, this gets annoying. Options: file `/feedback` asking Anthropic for the flag, or investigate whether MCP server-initiated notifications can replace Monitor as the wake mechanism (avoids the warning entirely, but unclear if Claude Code wakes on them).
- **Multi-message delivery**: if N messages arrive between Claude's turns, do all N surface or do they coalesce / drop?
- **Mid-typing behavior**: what happens to a notification fired while the human is mid-typing a prompt?
- **Stop hook + `additionalContext`**: could a Stop hook surface "you have pending bus messages" at natural turn boundaries as a complement to the Monitor signal?
- **Long-running reliability**: do monitors stay healthy across hours / days of session uptime? Docs say monitors don't auto-restart on crash — how do we recover?
- **Agent Teams as cautionary signal**: [open issues](https://github.com/anthropics/claude-code/issues/23415) show Anthropic's own multi-agent mailbox has unresolved delivery bugs. Do we inherit them? Where does our design diverge?

## Design

- **MCP tool surface**: final shape of `register`, `send`, `recv`. Acknowledgments? Read receipts? Conversation threading?
- **State format**: JSONL append-only? SQLite? File-per-message?
- **Agent identity**: what makes two sessions "the same agent" vs. distinct? Working directory? Manual name? PID?
- **Conversation continuity**: can a message reference a thread, and Claude pick up context from prior messages in it?
- **Self-deregistration on crash**: if a session dies without running the Stop hook, the registry has stale entries. PID-based liveness? TTL? Lease renewal?
- **Skill vs. description-as-policy**: which is the more robust home for trusted bus policy? Description is simpler; a skill is more discoverable and reusable.
- **Daemon lifecycle**: does the MCP server run per-user as a long-lived daemon (systemd / launchd) or spawn-on-demand from the first plugin connection?

## Out-of-scope (for now)

- Cross-host messaging (multi-machine mesh)
- Persistence across machine reboots
- Authentication / authorization between agents (single-user assumption)
- Streaming / long-form message bodies (current model is single-shot JSON messages)
