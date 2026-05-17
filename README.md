# Spanreed

An inter-agent message bus for Claude Code instances running on the same machine. Multiple interactive Claude Code sessions register themselves with the bus, can discover each other, and exchange messages — so a coordinator session (e.g. one that watches PR review comments) can dispatch work to the right worker session in the right repo.

> **Status**: Experimental. Validating that the underlying Claude Code primitives (plugin Monitor + MCP) can support the design. See [docs/findings.md](docs/findings.md) for what's been proven.

Named after the [spanreed](https://stormlightarchive.fandom.com/wiki/Spanreed) from the Stormlight Archive: a pair of magical writing tools that transmit text across vast distances. One side writes, the other side reads.

## Why

Coding workflows often span multiple repos and ongoing PRs. Running one Claude Code session per repo is natural, but those sessions can't talk to each other. The use case that drove this design: leave review comments on a PR, have the right Claude Code instance see them and address them, without having to juggle terminals or copy-paste between sessions.

## Architecture (sketch)

- **MCP server** — the bus API: `register_agent`, `list_agents`, `send_message`, `recv_messages`. State on disk under `~/.claude/spanreed/`.
- **Claude Code plugin** — wires up the MCP server, registers the session on start, and runs a [Monitor](https://code.claude.com/docs/en/plugins#add-background-monitors-to-your-plugin) that signals Claude when a new message arrives in her inbox.
- **Trust split** — monitor `description` carries the trusted policy; monitor stdout is an untrusted poke; the actual message body is data Claude reads and reasons about.

See [docs/architecture.md](docs/architecture.md) for the full design and [docs/open-questions.md](docs/open-questions.md) for what's still unresolved.

## Scope

Interactive Claude Code sessions only. Background / headless workers have other handles (Claude Code's Remote Control and `claude -p` cover those separately).

## Try the current experiments

```bash
claude --plugin-dir ./experiments/bus-test
```

The `bus-test` plugin is a minimal probe used to validate the underlying primitives — not the real bus. The actual MCP server and plugin haven't been built yet.
