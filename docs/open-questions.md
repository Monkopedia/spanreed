# Open questions

Things still to test, design, or decide.

## Behavioral / empirical

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
