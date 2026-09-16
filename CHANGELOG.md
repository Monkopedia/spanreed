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
- `--mode workspace | danger` selects confinement. `danger` removes the sandbox
  entirely and warns at startup and on every turn. (A `read-only` mode was cut
  before release: it auto-approved inside `--cwd` like every other mode, so its
  name made a guarantee this project has not verified.)
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
- **BREAKING: `spanreed list` now prints a human bus report by default, not JSON.**
  It shows attached peers and sync state; `--json` returns the previous array.
  See Upgrading below. Every surface that prints `last_seen`
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

**Two CLI contracts changed. Both have live consumers in this fleet.**

**1. Anything parsing `spanreed list` must add `--json`.** The default output is now a
human report; a script doing `spanreed list | json.load` will fail to parse it. This
was found by the review of #56, which identified a live consumer that takes its
unparseable-input path and renders an empty roster on upgrade — failing closed, but
failing.

**2. `spanreed send` no longer always exits 0.** It now exits **2** when the recipient
cannot be resolved (nothing was written) and **3** when the message was queued to a
registered agent whose session is not running. At `v0.1.0` it returned 0
unconditionally. A script branching on `spanreed send`'s exit status will now take its
failure path for mail that was in fact queued and will be delivered on restart — and if
it discards stderr, the line explaining that is lost with it. Treat exit 3 as *queued*,
not as failed; exit 2 is the only status that means nothing was written.

```bash
uv tool upgrade spanreed-bus
```

and `/plugin update spanreed` inside Claude Code — the plugin version moves in
lockstep with the package, and `/plugin update` compares the plugin's.
