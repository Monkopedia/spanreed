# Changelog

## 0.4.2

**Upgrading:** update the package and the plugin on every host, the same way as for 0.4.1.
**Recommended for anyone on 0.4.1 or earlier:** it fixes a regression you probably already
have (below). Nothing else needs to change.

### Fixes

- **Tool errors reach the agent in full again.** `uv tool install` resolves dependencies fresh
  rather than from spanreed's lock file, so an install of 0.4.1 or earlier made after `mcp`
  2.1 was published already runs a newer `mcp`. Since `mcp` 2.1, any error other than a tool
  error reaches the agent only as "Error executing tool <name>", losing the next step
  spanreed's errors give (call `list_agents`, start the bridge to a named host, why this
  session can't sleep). Every tool now returns its error as a tool error with the full
  message. (#104)
- **Locked dependencies refreshed** within the declared ranges, including `mcp` 2.3,
  `cryptography` 50 and `pydantic` 2.14. The lock governs development and CI, not what
  `uv tool install` gives users. (#48, #103)

### Behind the scenes

- More tests: the published `spanreed` and `spanreed-mcp` commands are run by name (#21,
  #112), the instructions a session receives at start are checked against the real tools
  (#41, #111), and the MCP identity tests run the code path production uses (#45, #102).
- `store.py` explains correctly why lock-free inbox appends are safe, and what can still tear
  one: a full disk, or the writer being killed mid-line. (#36, #106, #107, #109, #110)
- The test suite no longer prints a pytest-asyncio deprecation warning. (#105, #108)

## 0.4.1

**Upgrading:** update the package and the plugin on every host, as for 0.4.0. Nothing else to
do: sleepers created with 0.4.0 keep working, with no extra arguments.

### Sleepers: extra `claude` arguments on every launch

Anything after `--` on `sleeper create` is stored with the sleeper and passed to `claude` on
every launch, after spanreed's own flags. This gives each sleeper its own MCP servers or prompt
text without writing files into the folder it runs in:

```bash
spanreed sleeper create --name helper --cwd ~/some/worktree --permission-mode acceptEdits \
    -- --mcp-config ~/sleepers/helper-mcp.json --append-system-prompt-file ~/sleepers/helper.md
```

Only an allowlist is accepted: `--mcp-config`, `--strict-mcp-config`,
`--append-system-prompt[-file]`, `--system-prompt`, `--disallowed-tools`, `--effort`,
`--fallback-model`, `--verbose`, `--debug-file` and `--name`. Nothing on the list can widen a
sleeper's permissions past its `--permission-mode` or change which conversation it resumes.
Anything else is refused, so a flag Claude Code adds later stays refused until it is added on
purpose. Arguments are passed as given, so use absolute paths. `sleeper list` shows them.
(#100)

### Fixes

- **Registry rows recording pid 1 now read as stale.** Pid 1 (init on Linux, launchd on macOS)
  never exits, so such rows, written when a session's parent had already gone, were listed
  forever. One limitation: a Claude or Codex worker that really runs as pid 1, as a container's
  entrypoint, now reads as stale. (#98, #99)

## 0.4.0

**Upgrading:** update **both** the package and the plugin, and on every host. The plugin now
carries the turn hooks sleepers depend on. With an older plugin a sleeper wakes but never goes
back to sleep by itself; it says so with `needs_input`. Every host should run 0.4.0: an older
spanreed on the same machine can drop a sleeper's registry row (the sleeper record restores it),
and it reads an asleep sleeper as stopped. Ordinary sessions see one new MCP tool,
`request_sleep`, and three new hooks that start no Python outside a sleeper.

### Sleepers: agents that sleep when idle and wake on mail

A **sleeper** is a Claude Code session spanreed runs in tmux. When it has been idle for its
timeout, or asks to with the new `request_sleep` tool, its Claude exits and frees its memory.
It stays on the bus by name, and the next message to it **resumes the same conversation** and
delivers the mail there. Sleepers survive a reboot. (#85; design #84; built in #89, #90, #91, #93)

```bash
spanreed sleeper create --name helper --cwd ~/some/project \
    --permission-mode acceptEdits --model claude-opus-5-5 --idle-timeout 15m --trust
spanreed sleeper list | attach | wake | sleep | retire
```

- **Mail is never lost silently.** Whether Claude received a message is read from the
  session's own transcript, so mail it already has isn't sent again. The exceptions are a message it only
  partly saw, one cut by a compaction, or any message when the transcript can't be read:
  those are delivered again rather than risked. Mail that arrives while the agent is going
  to sleep is delivered before it sleeps. A line that still isn't delivered after 3 launches
  in which Claude ran (a corrupt line, say) is skipped so the rest can be. It is recorded
  for good, shown in `sleeper list`, and the sleeper is flagged `needs_input`.
- **Background work keeps it awake.** A shell or subagent it left running blocks the idle
  timeout. `request_sleep` (or `spanreed sleeper sleep`) stops that work and sleeps.
- **A failed wake is never silent.** An expired login, a rate limit, a missing `tmux`, or a
  Claude that exits at once leaves the sleeper asleep. It is flagged `needs_input` on the bus,
  the reason shows in `sleeper list`, and its mail stays unread for the next wake.
- **`--permission-mode` is required** and is passed on every launch, as is `--model` when
  given. `--trust` marks the folder trusted in Claude Code's own state, so the first launch
  doesn't stop at the "do you trust this folder?" dialog. It refuses your home folder and
  anything above it.
- It needs `tmux`. A woken sleeper starts from a clean environment, so it has no ssh agent.
  Managing sleepers on another host over `conjoin` is not built yet (#86); mail to one does
  wake it there.

### Fixes

- **A Codex worker refuses a `--name` that a running worker already holds.** Before, two
  workers on one name both ran every message. (#80)
- **`conjoin` no longer leaves a peer's agents looking live when nothing is connected.** A
  registry frame arriving just after a working link dropped (#81), or a peer hello arriving
  just after `--handshake-timeout` on a connect that never came up (#83, #92), used to
  mirror the peer's agents anyway.
- **A `CLAUDE_PID` of 0 or garbage no longer adopts another agent's identity**, including an
  asleep sleeper's. A registry row with an impossibly large pid no longer breaks
  `spanreed list`. (#50, #94)
- **Codex v2 exec approvals are logged under the method actually received**, not the v1 name.
  (#67, #96)

## 0.3.2

**Upgrading:** both machines in a `conjoin` pair should be on 0.3.2 before you pass `--timeout`
(see below). Otherwise there's nothing to do. Existing files are tightened to owner-only
automatically the next time each Claude session starts.

### `conjoin --timeout` and `--handshake-timeout`, for bad connections

A link silent for about 15 seconds used to be torn down and reconnected, with no way to be more
patient. Three separate timers enforced that: this end's watchdog, ssh's keepalive, and the far
end's watchdog. `--timeout SECONDS` sets all three, because raising only one does nothing while
the others still drop the link at 15s. The value is passed to the far end only when you set it,
so a peer on an older version keeps working as long as you don't use it.
`--handshake-timeout SECONDS` waits longer for a slow login on the far side. Both accept 6 to
86400 seconds. (#74)

### Bus state is private to your user

Everything spanreed writes is now created owner-only: directories `0700`, files `0600`. That
covers the registry, inboxes, cursors, peer records, config, activity log, MCP log and Codex
worker logs. Before, these were `0755`/`0644`, and the bus was private only where `$HOME` was
`0700`. The process umask is left alone, so `codex` and anything else spanreed launches still
create your project files normally. (#18, #75)

The tightening runs at **every** session start and applies to **everything under the state
root**, not only files spanreed created. If another tool keeps files there, now or later, they
become `0600`/`0700` too at the next session start, and any execute bit is removed. Symlinks are left alone. If that would break something you keep
under `~/.claude/spanreed/`, move it elsewhere before upgrading.

## 0.3.1

**Upgrading:** nothing to change for normal use. One edge: agent ids are now
validated, so a `SPANREED_AGENT_NAME` (or a Codex worker `--name`) containing
`@`, `/`, a space, or anything outside letters, digits, `.`, `_`, `-` is now
refused at startup with a message saying why. Previously it was accepted and
then misbehaved; see below.

### `conjoin` says what it is doing

`spanreed conjoin` now prints a timestamped line to its terminal whenever the
link changes state — connecting, connected to `<peer>`, disconnected after
`<uptime>: <reason>`, retrying in Ns, stopped. Before, a dropped bridge was
only visible by running `spanreed list` and finding the peer `DETACHED`. The
reason is the first cause observed (`the pipe closed`, `no frame from the peer
for Ns`, `shutting down`, or a refused peer). Only the end you started prints,
because the far end's stderr comes back over ssh to the same terminal. (#72)

### Fixes

- **Two peers on one host no longer crash `conjoin`.** Every bridge cursor was
  written through one shared temp file, so a host conjoined to two peers could
  race on it; the losing write raised past the reconnect loop and `conjoin`
  exited with a traceback instead of reconnecting. Each cursor now has its own
  temp file. (#47, #68) Two conjoins to the *same* peer can still collide;
  that is tracked as #69.
- **Agent ids are validated** wherever they enter the bus or become a path. A
  peer could address a message to a path outside the state root and have it
  written there, and a local name containing `@` was mistaken for a remote agent
  and deleted by the next sync. Both are now refused. (#12, #70)
- **`register_agent`'s tool description no longer says re-registering
  "replaces" the entry.** It keeps the name, working directory and focus, so
  an agent renaming itself that way silently got its old name back; the
  description now says so and points at `set_name`. `protocol.md` separately
  said re-registering changes "only" three fields; it also resets `status`.
  (#44, #19, #71)

## 0.3.0

### Codex worker: `--mode ask | auto | full`

**Upgrading:** if any script or unit file passes `--mode workspace` or `--mode
danger`, it will stop starting — argparse refuses the removed names rather than
mapping them. `workspace` is now `auto` (the default, so dropping the flag
entirely also works) and `danger` is now `full`.

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
