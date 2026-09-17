# Spanreed

An inter-agent message bus for local Claude Code sessions. Run one Claude session per repo; let them coordinate.

> **Status**: alpha. Published on PyPI (`spanreed-bus`) and in daily use for single-host, multi-session coordination. Cross-host messaging (`spanreed conjoin`) is experimental — see [Cross-host](#cross-host-experimental). Not yet on the official Claude Code marketplace.

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

## Cross-host (experimental)

By default the bus is single-machine. To bridge two machines' buses, run on one host:

```bash
spanreed conjoin <other-host>
```

This opens a persistent SSH pipe to the peer and mirrors each side's agents into the other's `list_agents` (as `agent-xxxx@host`); messages addressed to a qualified id route across. It reconnects on its own if the pipe drops, and runs in the foreground until you stop it — supervision (start-on-boot, restart-on-crash) is left to you (wrap it in systemd/launchd/tmux).

Check it with `spanreed list`, whose `PEERS` section shows every bridge and when each last synced its registry. Read that before concluding a peer's agent is gone: registry sync and message transport are independent, so a bridge can carry mail while leaving the far side unaddressable, and `list_agents` looks identical in both cases. The `list_peers` MCP tool exposes the same records to agents.

Prerequisites: `spanreed-bus` ≥ 0.0.4 on both hosts, and key-based non-interactive SSH (it reconnects unattended, so it can't answer a password prompt). Full setup, the most common SSH gotcha (non-default key name), and how to update `spanreed` on a peer host: [`docs/cross-host.md`](docs/cross-host.md). Design in [`docs/architecture.md`](docs/architecture.md#cross-host-the-ssh-bus-bridge).

Experimental and point-to-point only — no multi-hop routing or peer discovery yet.

## Codex workers (experimental)

A **Codex worker** is a bus agent with no human attached: a long-lived process
that owns one `codex app-server` thread and turns inbound mail into Codex turns.
It needs `codex` on `$PATH` and a signed-in Codex; it does **not** need the
Claude Code plugin, since there is no Claude session involved.

Run the doctor first. It exercises the whole path once and writes a single
self-contained log — send that file rather than a screenful:

```bash
spanreed codex --doctor --cwd ~/some/project
```

Then start a worker:

```bash
spanreed codex --name reviewer --cwd ~/some/project
```

`--cwd` is **required and has no default**. It is what the worker checks
approvals against and what it sends to Codex as `writableRoots`: in the default
mode an approval inside it is granted without asking anybody, any registered
agent may wake the worker, and the bus does not authenticate senders.

`--mode` mirrors Codex's own three permission modes rather than inventing a
fourth vocabulary:

| `--mode` | sandbox | approvals | answered by |
|---|---|---|---|
| `ask` | `workspace-write`, writes scoped to `--cwd` | granular | **you**, at the worker's terminal |
| `auto` (default) | `workspace-write`, writes scoped to `--cwd` | `on-request` | the worker, every decision logged |
| `full` | none (`danger-full-access`) | `never` | nobody; warns at startup and on every turn |

```bash
spanreed codex --name reviewer --cwd ~/some/project --mode ask
```

`ask` prompts on the worker's own terminal — never on the bus — and **waits
there indefinitely**: nothing is declined on your behalf, and until you answer,
that turn and every message queued behind it are stopped. Because of that it
**refuses to start when stdin is not a terminal**, since a worker with nobody
to ask would block on its first approval forever while still looking healthy.

**spanreed does not confine Codex.** It selects Codex's own sandbox and approval
settings, answers the approvals it is asked to answer, and logs every one.
Whether a selected sandbox actually holds is a property of `codex-cli`; that is
what `--doctor` is for.

**Experimental, and specifically so.** The protocol work is verified against
Codex's own schemas, and the failure paths are covered by fault-injection tests,
but the end-to-end path has been exercised against a stub rather than against a
live `codex app-server` on a machine where one is installed. Expect the doctor
to find things. See [docs/architecture.md](docs/architecture.md) §"Codex
workers" for the design and
[docs/findings.md](docs/findings.md) for what was actually measured.

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
