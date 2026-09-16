# Changelog

## 0.2.0

### Codex workers (experimental)

`spanreed codex` runs a bus agent with no human attached: a long-lived process
that owns one `codex app-server` thread and turns inbound mail into Codex turns.
A Codex session can now be a peer on the bus rather than a mailbox it cannot
read.

```bash
spanreed codex --doctor --cwd ~/some/project     # run this first
spanreed codex --name reviewer --cwd ~/some/project
```

- `--cwd` is **required and has no default**. It is the worker's entire blast
  radius: approvals are auto-approved inside it, any registered agent may wake
  the worker, and the bus does not authenticate senders.
- `--mode read-only | workspace | danger` selects confinement. `danger` removes
  the sandbox entirely and warns at startup and on every turn.
- `--doctor` exercises the whole path once and writes a single self-contained
  log, for machines where attaching a debugger is not an option.

**Experimental on purpose.** The protocol is verified against Codex's own
schemas (`AskForApproval`, `SandboxPolicy`, `ServerRequest`, vendored under
`experiments/codex-app-server-spike/schema/`) and the failure paths are covered
by fault injection, but the end-to-end path has only ever run against a test
stub. Expect `--doctor` to find things.

### conjoin: registry sync is bidirectional, and mail is no longer lost silently

Fixes [#55](https://github.com/Monkopedia/spanreed/issues/55).

- Registry sync is push **and** pull, so a peer's agents become addressable from
  the initiating host. Snapshots carry the counts behind the list, so
  "advertised zero agents" is distinguishable from "never answered".
- **A display name now resolves only among live agents.** Previously a name held
  only by stopped sessions resolved to a dead inbox and *reported success*,
  which is worse than a loud failure. It now refuses, and the dead inboxes stay
  empty.
- An exact `agent_id` for a stopped session still queues — dropping it would
  lose mail across a restart — but is never reported as delivered:
  `delivered_to_live_session` in the MCP result, exit code 3 from the CLI.
- Six distinct resolver errors replace the single message that said "Call
  `list_agents`" — advice that returned nothing and so confirmed the false
  belief that the agent had stopped.
- `spanreed list` is a human bus report showing attached peers and sync state
  (`--json` returns the previous array). Every surface that prints `last_seen`
  now prints beside it that nothing infers liveness from it.
- `list_peers()` added to the MCP surface.

The originally reported one-directional sync **could not be reproduced**; three
silent paths that each produce the reported state were closed instead, and the
non-reproduction is recorded in `docs/findings.md`.

### Also

- Identity is anchored to `$CLAUDE_PID`, so an agent keeps its identity across a
  `cd` and peers stop being confused by it.
- `conjoin` validates a peer's self-declared host before routing on it.
- `wait_for_reply`'s description no longer says the opposite of what it does.

### Upgrading

```bash
uv tool upgrade spanreed-bus
```

and `/plugin update spanreed` inside Claude Code — the plugin version moves in
lockstep with the package, and `/plugin update` compares the plugin's.
