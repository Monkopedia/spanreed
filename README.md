# Spanreed

An inter-agent message bus for local Claude Code sessions. Run one Claude session per repo; let them coordinate.

> **Status**: alpha. The architecture and primitives are validated (see [`docs/findings.md`](docs/findings.md)) and a working implementation is in place — pending end-to-end manual smoke test in real Claude Code. Not yet published to PyPI or the official marketplace.

Named after the [spanreed](https://stormlightarchive.fandom.com/wiki/Spanreed): a paired magical writing tool from the Stormlight Archive that transmits text across vast distances. One side writes, the other side reads.

## Install

Install the bus tooling from PyPI:

```bash
uv tool install spanreed-bus
```

Then launch `claude` in any directory and run these two slash commands at the prompt:

```
/plugin marketplace add Monkopedia/spanreed
/plugin install spanreed@spanreed
```

The MCP server (`spanreed-mcp`) and CLI (`spanreed`) need to be on `$PATH` for the plugin to find them — `uv tool install` handles this.

### Developing on spanreed

For hacking on spanreed itself, install from a local clone:

```bash
git clone git@github.com:Monkopedia/spanreed.git
cd spanreed
uv tool install --editable .
```

Then in Claude Code, point the marketplace at your clone instead of GitHub:

```
/plugin marketplace add /absolute/path/to/spanreed
/plugin install spanreed@spanreed
```

## Quickstart

Open two terminals, each in a different repo:

```bash
# Terminal A
cd ~/projects/repo-a && claude

# Terminal B
cd ~/projects/repo-b && claude
```

In terminal A, ask Claude to talk to the other session:

> Ask agent at repo-b what version of Node it's using.

Claude in repo-A discovers the peer, sends the question, the peer answers, the answer comes back. The conversation is visible in both transcripts.

## How it works

Two layers on Claude Code primitives:

- **MCP server** (`spanreed-mcp`, per-session) exposes the bus API as typed tools: register, send, recv, list, `wait_for_reply` with timeout.
- **Plugin** (auto-loaded) wires up a SessionStart hook for bus context, a Monitor for inbound wakeups, and points Claude at the MCP server.

State lives under `~/.claude/spanreed/` (registry + per-agent inboxes + per-session cursors). No central daemon — each session's MCP server is local and coordinates through shared files.

Read the full design in [`docs/architecture.md`](docs/architecture.md). Wire-format spec in [`docs/protocol.md`](docs/protocol.md).

## Update

The Python package and the plugin update independently:

```bash
uv tool upgrade spanreed-bus
```

For the plugin, inside Claude Code:

```
/plugin update spanreed@spanreed
```

## Development

See [`docs/development.md`](docs/development.md) for environment setup, running tests, and contribution conventions. The [`CLAUDE.md`](CLAUDE.md) at the root captures the discipline rules — Claude sessions working on this repo should read it.

## License

[Apache-2.0](LICENSE).
