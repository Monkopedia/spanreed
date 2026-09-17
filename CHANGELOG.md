# Changelog

## Unreleased

### Codex worker: `--mode ask | auto | full`

**`--mode workspace` and `--mode danger` are gone.** The modes now mirror
Codex's own three permission modes, because the thing being configured is
Codex's and a name invented here would have to be kept true to software this
project does not control. A stale `--mode workspace` or `--mode danger` is
**refused**, not quietly mapped: `danger` silently becoming a confined mode
would confine a worker its operator believes is unconfined. `--mode` is
rejected by argparse, which can only say "invalid choice", so `spanreed codex
--help` carries the mapping.

| old | new | what it does |
|---|---|---|
| `workspace` (default) | `auto` (default) | `workspace-write` scoped to `--cwd`, `on-request` approvals, answered by the worker and logged |
| — | `ask` | same sandbox, granular approvals, answered by **you** at the worker's terminal |
| `danger` | `full` | `danger-full-access`, `never`, nobody is asked; still warns at startup and on every turn |

- **`ask` prompts on the worker's terminal, never on the bus**, and waits there
  **indefinitely** — nothing is declined on your behalf, and until you answer,
  that turn and everything queued behind it are stopped, which the prompt says
  out loud. It therefore **refuses to start unless both stdin and the prompt
  stream are terminals**: a worker with nobody to ask would block on its first
  approval forever while still looking healthy in the registry. Both, because
  the prompt is written to stderr — `spanreed codex --mode ask 2> worker.log`
  leaves stdin a TTY, so a stdin-only check let exactly that worker start and
  then printed `THE WORKER IS BLOCKED` into the log file while it waited
  forever on a terminal showing nothing. Every prompt and every answer goes to
  the approval log.
- **Both sandbox levels are now sent, and the doctor sends them too.**
  `thread/start` takes `sandbox` (the `SandboxMode` enum) and `turn/start` takes
  `sandboxPolicy` (the object with `writableRoots`); the turn-level object alone
  was measured confining nothing on 2026-09-17 (recorded in
  [findings.md](docs/findings.md), with what that run does and does not
  establish). `--doctor` was sending only
  `approvalPolicy` and `cwd` at `thread/start` while reporting that it used "the
  worker's real params" — so it drove a thread that had been given no
  thread-level sandbox, and reported the absence of confinement it measured as
  Codex's. **Re-run the doctor**; a 0.2.1 run's step 4 was answering a question
  it had not set up.
- **spanreed does not claim to confine Codex.** It selects Codex's sandbox and
  approval settings, answers the approvals it is asked to answer, and records
  every one. Whether a selected sandbox holds is a property of `codex-cli` and
  is what `--doctor` is for. The docs no longer describe `--cwd` as the worker's
  blast radius; it is the value sent as `writableRoots` and the scope of the
  checks the worker performs itself.

## 0.2.1

Fixes to `spanreed codex --doctor`. **If you ran 0.2.0's doctor, re-run this one** —
0.2.0 could report a *false* PASS on the question the tool exists to answer.

- **Step 4 reported an approval it had not sent.** The verdict hardcoded
  "approved" while the handler may have declined, so *we declined and nothing
  escaped* printed as *we approved and confinement held* — the one reading that
  would suggest auto-approve is safe. It now reports the decision actually
  transmitted, and a decline is its own outcome that settles nothing.
- **The escape probe did not test an escape.** It asked for a write in the
  temp directory, which `workspaceWrite` leaves writable (`excludeSlashTmp`
  and `excludeTmpdirEnvVar` both default false) — so the write was expected to
  land with nothing asked, and the doctor called that a catastrophe and exited
  1. The probe now writes outside every writable root the run actually sends,
  computed from the policy rather than from `--cwd`.
- **A confirmed escape no longer reports the machine as healthy.** It is a PASS
  for step 4 — the round trip worked — and a *finding*, printed above the
  verdicts and reflected in the exit code, so `Everything passed` is
  unreachable while one stands. The exit code no longer depends on whether the
  worker happened to approve.
- **Step 3 could not fail.** It scanned the whole event stream for its marker,
  and the stream replays the user message, which contains the marker because
  the prompt asks for it. It now reads only the assistant's own output.
- `--doctor` captures app-server's log (0.2.0 captured nothing, because the
  client deliberately does not set `RUST_LOG` and the doctor did not either),
  skips step 4 under `--mode danger` rather than reporting the documented
  behaviour as a failure, cleans up its probe on every path including the
  timeout, and prints the effective `RUST_LOG` rather than claiming one.
- A `SKIP`ped or `WARN`ed step no longer prints `Everything passed`; that
  summary is derived from every step passing rather than from a list of
  known-bad verdicts. **The exit code is unchanged by this** — it is still 1
  only for a failed step or a finding, so a run whose step 4 was skipped exits
  0 with a banner saying so. If you gate on `--doctor`'s exit code, read the
  banner too.

### Upgrading

Same as 0.2.0 — see below. Nothing in the bus protocol or the worker changed;
these are diagnostics only.

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

- `--cwd` is **required and has no default**. It is what the worker checks
  approvals against and what it asks Codex to sandbox: approvals are
  auto-approved inside it, any registered agent may wake the worker, and the bus
  does not authenticate senders. What the sandbox then enforces depends on
  `--mode` — under `danger` there is none.
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

**If you pin `mcp`, `uv tool upgrade` will silently do nothing useful.** This release
requires `mcp>=2.0,<3`; `0.0.9` was the last release accepting `mcp<2`. With a 1.x `mcp`
pinned, uv resolves to the newest *compatible* `spanreed-bus` — 0.0.9 — and reports a
successful upgrade. Check with `uv tool list`; if it does not say `v0.2.0`:

```bash
uv tool list --show-with --show-version-specifiers   # find the pin
uv tool install --force spanreed-bus                  # reinstall without it
```

Otherwise:

```bash
uv tool upgrade spanreed-bus
```

and `/plugin update spanreed` inside Claude Code — the plugin version moves in
lockstep with the package, and `/plugin update` compares the plugin's.
