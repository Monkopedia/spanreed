# Open questions

Things still to test, design, or decide.

## Behavioral / empirical
- **A structural close for boundary prose** (recommended by the review of #56, not taken there): the
  guard in `tests/unit/test_codex_worker.py` is total on the *generator* axis — it walks every
  emitted string in every module of the package — and a **substring blacklist** (`FORBIDDEN`) on the
  *predicate* axis, so a newly-worded sentence making the same claim passes. Five review rounds
  produced five instances, and the last one shipped on a one-word difference (`the` worker vs
  `this` worker). The proposed close: *every emitted string containing `cwd` must be one of an
  approved set of module-level constants, checked by identity of the constant rather than by
  phrase.* That is total in the direction that matters — a new sentence about `--cwd` at a new site
  fails by construction, in any wording — and it reuses the AST walk already written. It was not
  taken during the #56 landing because it relocates prose written for five different audiences
  (model preamble, operator log, bus peer, CLI stderr, CLI `--help`) into one approved set, which is
  a refactor rather than a fix. Worth doing before the next feature adds a sixth audience.
- **An identifier can assert a property, and nothing reads a name as a sentence.** `read-only` was
  cut from `--mode` because it auto-approved inside `--cwd` exactly like every other mode; the
  guarantee was never in the code, it was in the identifier. Four rounds of auditing *prose* never
  reached it. The class to watch: `safe_*`, `validated_*`, `*_only`, and `CONFINED_MODES` itself.
  No guard is proposed — the observation is recorded because the defect is invisible to every
  mechanism this repo has.
- **An intermittent failure in `test_a_child_that_exits_zero_after_the_handshake_is_not_a_success`**
  (2026-09-16). Seen **twice**, both times inside a full `make check`, never under bare `pytest`
  and never in isolation: ~44 full-suite runs, 12 runs of its own module, and 18 runs under
  deliberate CPU contention all passed, and neither failure was captured with its assertion text.
  The test already documents a race with the child's exit and accepts two outcomes for *which*
  side notices first. A third path is the obvious hypothesis and remains unevidenced. What can be
  pointed at in the code rather than guessed: two of its assertions (`EXITED with status 0`, and
  the child's own final line) depend on the output drain having flushed, and `settle()` is a
  bounded wait by construction — so a real race exists there whether or not it caused these two
  failures. **Not fixed**, because a fix written against an unreproduced cause is what #55 taught
  this repo to avoid; recorded so the third occurrence is recognised as the third rather than the
  first.
- **`/reload-plugins` doesn't restart already-running MCP servers.** Reloading the plugin picks up changes to the manifest, hooks, monitors, and `.mcp.json` config, but the MCP server process spawned at session start keeps running on the *old* code. Adding a new MCP tool, renaming a tool, or changing tool signatures all require a full Claude session restart to take effect. Existing tools continue to work because the MCP server reads StateStore state fresh on each call — so behavioral changes to *implementations* of existing tools land on next call, but new tool *registrations* don't surface until restart. Development-workflow gotcha; possibly worth `/feedback` asking for `/reload-plugins` to restart MCP server processes too.
- **Monitor exit-warning UX**: a plugin Monitor shows up as "1 monitor still running" in the status line and triggers a "background work in progress" warning on session exit. There's no documented way to mark a monitor as essential infrastructure that should be quietly ignored on exit (no `essential` / `silent` / `quiet` field; no settings override). For an always-on bus, this gets annoying. Options: file `/feedback` asking Anthropic for the flag, or investigate whether MCP server-initiated notifications can replace Monitor as the wake mechanism (avoids the warning entirely, but unclear if Claude Code wakes on them).
- **Multi-message delivery**: if N messages arrive between Claude's turns, do all N surface or do they coalesce / drop?
- **Mid-typing behavior**: what happens to a notification fired while the human is mid-typing a prompt?
- **Stop hook + `additionalContext`**: could a Stop hook surface "you have pending bus messages" at natural turn boundaries as a complement to the Monitor signal? (Also relevant to agent status: a Stop hook could set `status` to `idle` at turn end, making the `idle`/`working` distinction reliable instead of best-effort — see the "Agent status" section in `architecture.md`. Deferred for v1; the human-need levels work without it.)
- **Long-running reliability**: do monitors stay healthy across hours / days of session uptime? Docs say monitors don't auto-restart on crash — how do we recover?
- **Agent Teams as cautionary signal**: [open issues](https://github.com/anthropics/claude-code/issues/23415) show Anthropic's own multi-agent mailbox has unresolved delivery bugs. Do we inherit them? Where does our design diverge?

## Design

- **MCP tool surface**: final shape of `register`, `send`, `recv`. Acknowledgments? Read receipts? Conversation threading?
- **Codex on the bus** (answered empirically, 2026-09-15, and the answer is *yes*): a separate process can spawn `codex app-server --listen unix://…`, complete the `initialize` + `initialized` handshake over its WebSocket control socket, list threads, **create its own thread, and drive a turn in it to completion** — the model's reply came back on `codex-cli 0.154.0` through `item/agentMessage/delta` and `turn/completed`. So a Codex session can be a real bus peer, woken by inbound mail, not a mailbox. **The limit that is real:** a thread a human has open is `flock`-held by that process for the life of the session, so spanreed must *own* its thread rather than join one. That is the supported shape anyway. **The earlier "not yet" answer was wrong, and so were the eight causes proposed for it** — the hangs were caused by the spike itself: it spawned app-server with an output pipe, set `RUST_LOG=info`, and read that pipe only after the run, so the 64KB buffer filled and blocked the server inside whatever handler was logging. Every retraction and the full record is in `experiments/codex-app-server-spike/README.md`; see also [findings.md](findings.md#test-5-can-a-foreign-process-drive-a-codex-turn-2026-09-15). The **design** that follows from it — lifecycle, the three permission modes, and what `--cwd` does and does not bound — is in [architecture.md](architecture.md#codex-workers).
- **Approval `decision` wire values** (answered 2026-09-16, from the vendored schema): the two generations spell them differently and the code originally sent a value belonging to neither. v1 (`ExecCommandApprovalResponse`, `ApplyPatchApprovalResponse`) uses `ReviewDecision`: `approved` / `approved_for_session` / `approved_mcp_policy_amendment` / `timed_out` / `abort` — there is **no** `decline`, the negative is `abort`. v2 (`CommandExecutionRequestApprovalResponse`, `FileChangeRequestApprovalResponse`) uses `accept` / `acceptForSession` / `decline` / `cancel`. `codex_approvals.wire_decision()` maps method → member, and a test asserts the string `approve` is never emitted. See `experiments/codex-app-server-spike/schema/ServerRequest.json`.

- **`baseInstructions` vs `developerInstructions`** (decided, flagging for review): `architecture.md`
  named both for `--instructions`. The worker sends `developerInstructions`, because
  `baseInstructions` *replaces* Codex's built-in agent prompt — a one-line persona would silently
  strip the model's tool and sandbox guidance. If the owner wanted the replacing kind, this is the
  knob.
- **A Codex worker's log failing mid-run** (decided one half, flagging the other): an unwritable log
  at **startup** is a refusal to start — approvals must be recordable. A log that stops taking
  writes **later** (a full disk, a permission change, an unmounted state root) currently falls back
  to stderr, says loudly that approvals are no longer being recorded durably, counts the failures,
  and the worker **keeps going**. The symmetrical choice would be to stop, as startup does. Left
  running because a worker mid-queue has senders waiting on replies, and stderr on a supervised
  process is usually captured — but if the owner wants the same refusal in both places, this is the
  knob. Pinned by `test_a_log_that_becomes_unwritable_mid_run_keeps_the_line`.
- **A Codex worker whose `--cwd` disappears**: it refuses every turn, replies to each sender with
  the reason, and stays on the bus, on the theory that the directory may come back (a worktree
  swapped, a mount that dropped). The alternative is to exit, which is what it does when the
  transport dies. Nobody has hit this in practice yet, so the choice is recorded rather than
  defended.
- **Single-sourcing published tool descriptions**: `MCPServer.tool()` accepts `description=`, so a tool's published description could be *sourced from `store.py`'s docstring* rather than restated in `mcp_server.py`'s. Raised by the review of #42 and deferred rather than decided.
  - **Note what is NOT the problem.** An MCP tool's published description already *is* its docstring, byte for byte — verified against `mcp_app.list_tools()`. That is not a gap; it is why #14 was severe, since a wrong docstring is automatically a wrong published description with no step in between at which anyone would notice.
  - **What it fixes.** Not "the two copies disagreeing" — that framing is wrong, and getting it wrong is how #14 stayed live. #14's harm was **asymmetric visibility**: the false copy was the published one (in every agent's context) and the true copy was in `store.py`, which no agent reads. Single-sourcing puts the human editing the docstring in front of the exact bytes the agent receives, so the reader best placed to catch an error finally looks at the published artifact.
  - **What it would fix, and #44 is the case for it.** The two copies that can disagree are `mcp_server.py`'s docstring and `store.py`'s. #44 is exactly that: `store.py:160-170` is right about upsert semantics, `mcp_server.py:58-59` — the published copy — says "replaced (upsert) — same semantics as the underlying store", and `protocol.md:233` agrees with the store. Both Python, one wrong, and had the published text been sourced from the store's docstring #44 could not have been written.
  - **What it does not fix.** `docs/protocol.md` remains a third copy outside Python's reach, so this addresses two of #14's three locations. And a single wrong string is still wrong — it removes the asymmetry, not the falsehood — so it never substitutes for a description guard.
  - **Suggested scope if taken**: the few tools carrying load-bearing semantics (`wait_for_reply`, `register_agent`), not file-wide. Related: #14, #42, #44.
- **`session-start` under an inherited `SPANREED_AGENT_NAME`** ([#123](https://github.com/Monkopedia/spanreed/issues/123)): the `_ensure_registered` stub keeps `getppid()` under the override because child processes inherit it (owner decision on #38, 2026-11-25), but `session-start` records `$CLAUDE_PID` under the same override. A Claude started from inside an override session therefore writes its own pid onto the parent's row at its start. The sleeper runner relies on that write, so it cannot simply be refused. **Recommendation:** accept it at session start and say so beside the `session-start` row in `protocol.md`. A child that inherited the override also resolves as that id and reads its inbox, so its row tracks a Claude that is actually serving the id; the stub runs from any descendant process, Claude or not. Pinned as current behaviour by the `session-start, override set` row of `tests/unit/test_pid_invariant.py`.
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
- **Reconnect**: `spanreed conjoin` is a foreground command that re-establishes the pipe with exponential backoff + jitter when it drops. Death is detected via EOF, SSH keepalive (`ServerAliveInterval`), and a receive-side watchdog (no frame within `recv_timeout`). A dropped link keeps the mirrored entries, so mail for the peer queues while `conjoin` retries; `conjoin` clears them, and kills the SSH child, when it exits on SIGINT/SIGTERM or `--max-reconnects` (#34). Supervision (start-on-boot, restart-on-crash) is deliberately **out of scope** — wrap it in systemd/launchd/tmux if you want a service.
- **Registry sync direction** (resolved 2026-09-16, issue #55): sync is **push and pull**. Each side advertises on a timer *and* sends `registry-request` until it has actually received a snapshot, so no side depends on the other's timer for state it needs. Every previously-silent drop (a frame before the handshake, a frame this version cannot model) is now recorded rather than swallowed, the `registry` frame carries the counts behind its list so "advertised zero" is distinguishable from "never answered", and each bridge writes `peers/<host>.json` so the whole state is readable from `spanreed list` on either host. Design in [`architecture.md`](architecture.md#registry-sync-push-and-pull).
- **Delivery across a drop**: at-least-once. Outbound advances the `*@peer` cursor only after a confirmed send (no loss); `append_message` dedupes by `msg_id` on delivery (no double-delivery on resend). Validated: a serve killed repeatedly drops then re-mirrors its agents as the bridge respawns.

Still open:

- **Multi-hop / transitive routing** (B reaching C through A): deferred. v1 is point-to-point only.
- **Peer-host discovery**: v1 launches bridges manually per pair. A peer-host config/list is future work.
- **Peer up/down signal to agents**: when a peer connects or drops, should local agents be *notified* (a bus event), or only observe it by polling (`list_agents` + `list_peers`)? Currently the latter. #55 narrowed this but did not answer it: the state is now *readable*, so an agent that thinks to look can see a bridge attach or die — nothing wakes it to look.
- **Registry-sync cadence**: poll interval / change-detection for the `registry` frame; tradeoff between freshness and chatter. #55 added a `registry-request` pull that re-fires each tick *only* while a side has never received a snapshot, so the steady-state cadence is unchanged; whether the push should become change-driven rather than timed is still open.
- **Queue-vs-refuse for a stale exact id** (decided in #55, flagged for review): `send_message` to a display *name* that matches only stopped sessions now **raises** — that was the silent-loss case. To an exact `agent_id` whose session has exited it still **queues**, because mail waiting for a restarting session is a documented property, and the sender is told (`delivered_to_live_session: false`, CLI exit 3). The alternative — refuse both — would lose mail across a restart. If the owner wants refusal there too, this is the knob. A mirrored `<id>@<host>` id is the decided exception: once no `conjoin` for that host runs, it is refused (#34).
- **The serve end refuses during a drop** (from #34, recommendation: accept). The owner's rule keys queuing on a running `conjoin` for the host. The host that was *dialled* runs no `conjoin` of its own, only `conjoin --serve`, whose process ends with its link. So during an outage the dialling host queues mail for the dialled one, but the dialled host refuses mail for the dialling one until the reconnect re-syncs, even though the dialler is retrying. The serve end cannot know whether the dialler will come back, so it has nothing to queue on. Recommendation: accept the asymmetry, and run `conjoin` from the host whose outbound mail matters more; revisit if both directions need queuing, which would need the dialler's liveness to be visible to the serve end.
- **Before the first connection, nothing is addressable** (from #34, recommendation: accept). A `conjoin` that has never connected has mirrored no rows, so a send to `agent-X@host` is refused as unknown even though `conjoin` is running. Queuing it would mean accepting an address no snapshot has vouched for, which reopens the typo black-hole `c231813` closed. Recommendation: keep refusing.
- **`list_agents` does not mark a reconnecting peer's agents** (from #34, recommendation: leave as is). While `conjoin` reconnects, the peer's mirrored rows read as live in `list_agents`, because the decision keeps them addressable. Nothing on the `Agent` record says the link is down; `list_peers`, `spanreed list` (`RECONNECTING`) and the send verdict do. Recommendation: leave the record unchanged, since the send verdict already tells the sender the mail is only queued.
- **A peer that comes back under a different host label** (from #34, recommendation: accept). `conjoin` owns every row it mirrored, under every label a peer has declared to it, until it exits. If the peer reconnects under a new `--label` or hostname, the old label's rows stay addressable and queue mail nothing will forward until `conjoin` exits. Recommendation: accept; relabelling a peer mid-run is rare and restarting `conjoin` clears it.
- **Two bridges sharing one host's peer record** (from #122, recommendation: accept two-way, refuse a second `connect`). `peers/<host>.json` is keyed only by host and both ends of a bridge write it, so two live bridges can share it: by design when `conjoin` runs in both directions (this host's `connect` and the `serve` the peer's `conjoin` started here), and by accident when two `conjoin`s to one host run. The record describes whichever wrote it last. The resolver's #122 refusal applies only to a row owned by the bridge the record names, so a row the other live bridge mirrored still resolves. What remains: when the record's bridge stops, the send verdict, which judges by the record, says `NOT DELIVERED: NO BRIDGE IS RUNNING` although the other bridge forwards the mail, until that bridge next writes the record (within a sync tick while it is attached; up to its reconnect backoff, about 45 s, while it is reconnecting). `list_peers` and `spanreed list` read the same record and say the same. Recommendation: accept this for the two-way pair; have `conjoin` refuse to start only when the record's live, not-stopping owner has `role: connect`, which leaves two-way `conjoin` working. That check reads only whichever bridge last wrote the record, so it is not complete: if the `serve` end of a two-way pair wrote it last, the record names a `serve` owner and a duplicate `conjoin` to the same host still starts. Closing that needs a record of the `connect` bridge kept apart from the one `serve` writes, which is not proposed here.

## Remote sleeper management (#86, being built)

Design in [`architecture.md`](architecture.md#managing-a-peers-sleepers-over-conjoin-86), wire
format in [`protocol.md`](protocol.md#pipe-frames). The owner's decisions of 2026-11-25 are
recorded there, under "Decisions", with the words that are his marked as his. They settled the
carrier (frames over `conjoin`), the grant (one per-machine switch,
`spanreed sleeper remote-access on|off|show`, off by default), and the first draft's R1, R2 and
R5 to R8; R3 and R4 were superseded by the single switch. No cap on a remote create beyond a
local one, and a person at a terminal to turn the switch on, come from a design summary the
author proposed and the owner answered "yes" (14:47:09Z); the summary is quoted on #86, in the
comment of 2026-11-25 14:59.

Decided after review of #119:

- **R9. The numbers behind request expiry.** The owner answered "recs fine" to the
  recommendations (#86, comment of 2026-11-25 14:54): `--timeout` is capped at 600 s, which is
  also the longest a request lives; the clock allowance for skew between the two hosts is a
  fixed 30 s; and expiry is checked again when a queued request starts, answering `expired`
  instead of running it.
- **R10. The TTY check on `remote-access on`.** Accepted for v1 ("recs fine", same comment), and
  stated as protection against **accidents, not a boundary against agents**. The switch is a
  key in `config.json` that any process running as the user can write directly, without the
  command; and the command itself can be driven through `tmux send-keys` or `script(1)`. Asked
  whether to accept that, the owner: "I mean this is true of all things, claude can rewrite its
  own settings in the right environment, this is how the agentic world works and there's no
  inherent way around it without crazy security controls" (#86, comment of 2026-11-25 14:59).
  Nothing stronger, such as a root-owned file, is added. `architecture.md`, decision 7.
- **What the grant amounts to.** In the spanreed session (his words, recorded on #119 by the
  coder in the comment of 2026-11-25 15:39), the owner called it roughly equivalent to the ssh access a conjoin implies, but bidirectional, and
  "this is expected"; and said trust is transitive across the conjoin graph, without changing the
  model: "when you conjoin with someone, you trust their entire bus/network". `architecture.md`,
  decisions 9 and 10.

Still open:

- **Calls stage 1 had to make where the design is silent** (#86 stage 1, the target's side).
  Each is built as recommended, so the code has a default; each is the owner's to change.
  - *Where the switch is read relative to the idempotency record.* The design lists "checks the
    switch, checks expiry, writes the record" for a new request, and says an existing record
    "decides, whatever the frame's `expires_at` says", without saying whether it also decides
    whatever the switch says. Recommendation, built: a resend of a request already recorded is
    answered from its record even if the switch has since been turned off, since answering
    `refused` for a create that ran would report it as never allowed. The switch is read
    wherever a request could *start*: a new request, one re-queued from a dead bridge process,
    and when the worker takes one off the queue.
  - *A request waiting in the queue when the switch is turned off.* The design says an
    operation already running finishes, and is silent on one still queued. Recommendation,
    built: it is answered `refused` and not run, the same re-check the worker makes for expiry.
    It fails closed, and it means a create never runs without the `PATH` the switch recorded.
  - *`requested_by` that is missing or not a string.* The design lists no check for it.
    Recommendation, built: `invalid`, like any other field the target cannot model. It is
    logged, never trusted, so this costs a correct requester nothing.
  - *A request re-queued from a dead bridge process writes no audit line of its own.* Its
    `received` line came from the dead process, and its outcome line names the bridge that ran
    it. Recommendation: keep; add a `requeued` event if a log ever proves hard to follow.
- **Default-on after a few releases?** Deferred by the owner on 2026-11-25: "that sounds fine for
  now, maybe after a few releases we turn it on by default". Revisit once remote management has
  shipped and been used. Turning it on by default would also turn on what "What turning it on
  grants" describes, for every user who runs `conjoin`.

## Adopting a running agent as a sleeper (designed, not built)

Design in [`architecture.md`](architecture.md#adopting-a-running-agent). The request came from
the owner on 2026-11-25, as main-coordinator relayed it. It settles three things: the adopted
conversation is resumed, the bus id is kept (not shown as hex), and the handover has no double run
and no missed mail. Each question below (A1–A15) is open, and each has the author's recommendation. The
design doc is written as if every recommendation is taken.

- **A1. Which handover.** (A) Refuse while the session is live, so someone ends it and then
  adopts. (B) The session adopts itself, and a waiter at generation 0 takes over when it exits.
  (C) `create` ends the live session itself and commits, in one bounded call. *Recommendation:*
  C for v1 and the pilots. No mail goes unseen and there is no double run, and no process
  outlives the command. It does not fully rule out ending a turn that is handling mail: see
  A2. A is the same command when the session has already exited. B can come later as
  "C, run detached once the current turn ends", if agents should put themselves to sleep. B
  needs its own marker, because the orphan rule as built reports every send as a wake failure.
  It also has no way to see the live session's turns, because its hooks don't report them.
- **A2. Ending a live session: opt-in, only when idle, and what to do if it stops being
  idle.** The live Monitor keeps delivering mail during the handover, so a single idle check
  at the start can pass and a turn can then start and be cut off by SIGTERM. If that turn
  already has an assistant record, its message counts as read and is not replayed.
  *Recommendation:* (1) end it only with `--end-session`, and otherwise refuse while it is
  live. (2) Before anything else, require idle: transcript unwritten for 60 s and its last
  record ending a turn. (3) After writing the record, stop the live Monitor with `SIGSTOP`, so
  no new mail can start a turn, wait 10 s, and check idle again. (4) **If it is not idle,
  abort**: `SIGCONT` the Monitor, delete the record, wait 30 s, then list every line
  appended since the inbox snapshot `S` taken when `create` started, up to the moment of
  listing, by line and `msg_id`, marked delivered or not, and exit non-zero. (It starts
  at `S`, not at the freeze, because a line appended just before the freeze that `tail`
  had not yet read also comes out in the burst.) The listing goes to the output, the
  activity log, and an abort report that `sleeper list` shows.
  **What aborting costs (corrected; the previous version said nothing was lost).** A
  stopped `tail` emits everything that arrived during the freeze as one burst when it is
  resumed (measured by the reviewer of #129). Claude Code merges a burst into one event and
  cuts it at about 3,000 characters, and the lines past the cut never reach Claude
  (Test 9). So six or more long messages during a 10–25 s freeze can lose mail to the live
  session, and the abort path is taken exactly when the agent is busy. The abort can
  promise only that every such line is shown as delivered or **listed**, never that it
  reaches the session. Committing has no such cost: the sleeper replays those lines one at
  a time. The choices for (4):
  - (a) **abort and list** (recommended for v1): bounded, and the session is deaf only for
    the freeze. Mail can miss the session, but never silently.
  - (b) **wait, bounded, for the turn to end, and check again**: more likely to commit, and
    a commit replays everything. But the session is deaf for as long as it waits, and if the
    wait runs out the burst it then aborts with is larger.
  - (c) **abort and re-feed**: as (a), but `create` also re-appends each line not shown as
    delivered to the inbox, 3 s apart, so the resumed Monitor delivers each as its own
    event. It is the only option where the session receives the mail, but it writes a
    second inbox line with the same `msg_id`, where today a `msg_id` names one line
    (`recv_messages(since_msg_id)` and cursors find a position by it), and it re-sends
    other agents' mail. That is a wire-format change, so not for v1.
  Even with all of this, a turn that runs undetected (a long foreground tool call that writes
  nothing, or a turn started by a person typing in the pane in the last moment) can still be
  cut off, so the design claims "no message goes unseen" only when the adoption commits, and
  never "no message is interrupted". SIGTERM mid-turn is unmeasured (Test 8 was at idle).
  Claude Code's `sessions/<pid>.json` `status` (`busy`/`shell`) might be a better idle
  signal, but it is undocumented and unmeasured.
- **A3. CLI shape.** *Recommendation:* both forms. `--session-id`/`--agent-id` (with `--name`
  and `--cwd`) is the primitive. `--adopt AGENT` fills them in from the agent's registry row and
  Claude Code's `~/.claude/sessions/<pid>.json`, used only when `procStart` matches the row's
  `pid_start`. If that doesn't work, it refuses and asks for the explicit flags. It never copies
  the permission mode (decision 2), the model or `claude_args`, and it prints what it took.
- **A4. `read_through` when the transcript shows no delivery at all**, for example a
  conversation just after `/clear`. *Recommendation:* start after the last inbox line, up to the
  snapshot `S` (A5), that was stamped on this host and whose `ts` is older than the
  transcript's first record. Mail from before the conversation is history, and mail during it
  without a delivery is replayed. Lines relayed from a peer carry the sending machine's clock,
  so their `ts` is never compared; they are placed by their position in the file. The
  alternative, the end of the inbox, could lose mail that arrived during the handover.
- **A5. Undelivered lines before the high-water mark.** These are mail the live session missed
  while awake, mostly to the Monitor's ~3 KB batch cut. The request does not address this;
  the recommendation is the author's. *Recommendation:* treat them as history, and have
  `create` list them by line and `msg_id` so a person can forward them. Replaying them would
  also bring back mail from earlier conversations under the same id. **This applies only up
  to the inbox snapshot `S` taken when `create` starts.** A line that arrives after `S` is
  never history, even if a later line was delivered, so mail dropped during the handover is
  replayed rather than buried.
- **A6. Waking at adoption.** `create` launches nothing today (a stage-1 call by the author).
  *Recommendation:* an adoption that finds unread mail after `read_through` wakes the sleeper
  straight away, through the ordinary wake path. Otherwise mail from the handover waits for the
  next send.
- **A7. How a woken adopted sleeper knows its id.** *Recommendation:* the runner sets a new
  `SPANREED_AGENT_ID`, which outranks `SPANREED_AGENT_NAME` when an identity is minted. It is
  validated, unqualified, and set on every launch; for a record whose id is `agent-<name>` it
  equals what is minted today. The alternative is to read the id from the record whenever
  `SPANREED_SLEEPER=1`. That would make minting depend on shared state. Either way it grants
  nothing new: `SPANREED_AGENT_NAME` can already mint any `agent-<x>`.
- **A8. A new session in the sleeper's directory takes its id.** All three pilots have ids
  derived from their cwd, so plain `claude` in `~/git/hauler` mints `agent-eac78acd`. The
  SessionStart upsert puts it on the sleeper's row, and two conversations then answer the same
  mail. This exists for every sleeper today, but it is hard to reach. *Recommendation:*
  `session-start` and `inbox-watch`, in a session without `SPANREED_SLEEPER`, refuse an id that a
  local sleeper record holds. The session stays off the bus, and its context says why and how to
  join under another name.
- **A9. A hand-run `claude --resume` of a sleeper's conversation.** *Recommendation:* before
  every launch, also refuse when a live pid's `~/.claude/sessions/<pid>.json` names the record's
  session id, the same way the orphan rule refuses. This applies to all sleepers. It is
  best-effort, because the file is internal, and it is never the only guard. Adoption runs it
  too, together with a check that no other process is tailing the id's inbox (and, for a
  cwd-derived id, that no other live session has that cwd). That covers a second session that
  is already running when the agent is adopted, which A8 does not.
- **A10. The adopted agent loses its shell environment**, `SSH_AUTH_SOCK` included. That is the
  sleeper allowlist ("Environment"), and the gap is already known. But a repo agent that used to
  push over ssh stops being able to. *Recommendation:* accept it for the pilots and check first
  whether hauler, sdbus-kotlin and lsp-kotlin push over ssh. A per-sleeper environment
  passthrough would be a separate decision.
- **A11. Adopting over `conjoin`.** *Recommendation:* refuse the adopt flags with `--host` in
  v1. Remote management (#86) is not built, and ending a session on another machine is a step
  past what decision 4 of #86 covered.
- **A12. What the replaced row keeps (minor).** *Recommendation:* keep the display name (from the
  record) and the focus, and clear the status, as any re-registration does.
- **A13. May adoption stop background work, or must it wait for it?** Decision 7 lets
  background work be stopped only when the agent itself asks to sleep (`request_sleep`); the
  idle timeout never stops anything. Adoption is an outside actor ending the session without
  the agent asking, which is closer to the idle-timeout case, but decision 7 did not consider
  it. **Corrected premise:** the previous version called detection weak, saying a background
  shell writes nothing to the transcript until it ends. That was wrong. Every background
  launch is recorded in the session's own transcript ("Async agent launched", "Command
  running in background with ID: …"), and every completion returns as a
  `<task-notification>` with the same `<tool-use-id>` and a `<status>`. On the
  coordinator's transcript the reviewer of #129 counted 104 launches and 101 completions,
  and the 3 unmatched launches were exactly the 3 tasks in flight. So a launch since the
  current process started with no completion is a direct "busy" signal; descendant
  processes stay as a backstop. It is measured on one transcript, and whether every kind of
  background work leaves both records is still to check. A miss is not safe: step 5 kills
  the work it missed. *Recommendation (unchanged, now on firmer ground):* wait. Refuse to
  adopt while any background work is in flight, and name each launch or process found. The
  alternative is a `--stop-background` flag that stops it, as `request_sleep` does, and
  logs what was stopped; with the pairing, `create` could now name exactly what it would
  stop, which makes that alternative more practical than before, but it is still an outside
  actor stopping work the agent started.
  Two edges of the detector, each with the author's recommendation (review 6061400830):
  - *Monitors.* Most Monitor-tool notifications carry no `<tool-use-id>` (19 of 21 on the
    measured transcript), so pairing them would refuse adoption long after a Monitor
    ended. *Recommendation:* leave Monitor launches out of the pairing and judge them by
    the descendant-process backstop, which sees a running Monitor as a process; `create`
    names it and says to stop it or wait. Alternatives: treat every unpaired Monitor as
    blocking (fails safe, but can block adoption until a restart), or match Monitors by
    another key (none is known).
  - *Work launched before a `/clear`.* The new transcript does not hold it, and a
    background agent runs inside the `claude` process, so the backstop cannot see it; a
    miss kills it at step 5 and its result is lost. *Recommendation:* pair launches and
    notifications across every transcript in the project folder written since the process
    started (and their subagents), which cannot miss a launch this process made and can
    only over-refuse. Alternative: refuse whenever the adopted transcript began after the
    process started, until the session restarts; simpler, but it blocks every agent that
    has used `/clear`.
- **A14. Who may run `--end-session`?** No command today ends another agent's live session.
  With it, the coordinator (or any process running as the user) can end any idle local agent.
  Spanreed cannot enforce who runs a CLI command (decision 7). *Recommendation:* the owner, or
  an agent acting on the owner's instruction that names the target agent; the coordinator does
  not adopt on its own initiative. `create` records the caller's asserted identity in the
  record and the activity log. It refuses when the caller is inside the target session
  (that is self-adoption, B). Where that rule lives (fleet policy, the coordinator's
  instructions) is for the owner.

- **A15. An interrupted `create`.** Revised after review 6061400830, which found that the
  previous recovery could not tell "`create` killed before it began ending the session"
  from "killed while the session was ending", and prescribed opposite actions for the
  two; that the `frozen_monitor` write was not ordered before the `SIGSTOP`; and that the
  recovery ran its 30 s wait inside whichever sender triggered it. Revised again after
  review 6061584298, which found that a handler taking the sleeper lock could fire while
  `create` already held it and wait on itself forever. Revised again after review
  6061710901, which found that the abort deleted the record before writing its report,
  so a death in the 30 s wait left nothing naming the unchecked lines. The design now:
  - `create` installs its SIGINT/SIGTERM/SIGHUP handler before it writes the record. The
    handler only records the signal; `create` acts on it at fixed checkpoints, all
    outside the lock (after step 2's release, before step 3 takes the lock, after step
    3's release, throughout step 4, and just before step 5's marker write).
  - Step 3 writes `adopted.frozen_monitor` (pid, start time, `F`) under the lock and only
    then sends `SIGSTOP`, so a stopped Monitor is always on record.
  - Step 5 writes a **phase marker**, `adopted.ending` (time, and the descendants it will
    signal), under the lock before its first SIGTERM.
  - A record in `sleeping` at generation 0 with a dead runner is an interrupted adoption,
    and only that. Its **recovery** branches on the marker. With no marker and the
    session alive, it aborts: resume the recorded Monitor, delete the record, wait 30 s,
    list every line since `S`. With the marker, the session is ending: send it SIGTERM
    (the recovery cannot tell whether `create` had), SIGKILL it if it outlives 15 s from
    that SIGTERM, SIGKILL the recorded descendants and stopped Monitor whatever the
    session's state, then complete the commit
    (`read_through`, `asleep`, wake if unread); the record is kept.
  - The listing of an abort goes to the activity log and an abort report,
    `sleepers/<name>.adopt-aborted.json`, which `sleeper list` shows until it is dismissed
    or the next adoption commits; and to the output when there is one. The report is
    written as `listing: pending` before the record is deleted and completed after the
    wait, so a death in the wait leaves it pending, and `sleeper list` or `--recover`
    finishes it; a pending report is never dismissed. The recovery uses the same
    record-only signal handler, acting on a signal only before it changes anything.

  Four points need a decision.
  (i) **A signal after the phase marker** cannot be undone, because the session is
  already being ended. *Recommendation:* leave it recorded (from the marker write on,
  `create` reaches no checkpoint that acts on it), finish the bounded commit (about 15 s
  plus the escalation), then exit with the signal's status. The alternative is to exit
  at once, at a check after the marker write, and leave the record to the recovery.
  (ii) **Who runs the recovery.** It waits up to 30 s plus an exit, and the wake path
  runs inside every inbox writer (MCP `send_message`, `spanreed send`, a Codex worker's
  reply, a `conjoin` bridge's frame loop), so it must not run there. Options:
  (a) the wake path launches `spanreed sleeper adopt --recover NAME` detached, in a new
  tmux session as a runner is, records it as the record's runner (so later sends do
  nothing, by the existing rule), and returns at once; it launches at most one per record
  and falls back to (b) if that one dies;
  (b) the wake path only records a wake failure and `needs_input` naming `spanreed
  sleeper adopt --recover NAME`, and a person runs it.
  *Recommendation:* (a). The harm is a session that hears nothing, and the first mail is
  when it matters; (b) leaves the session deaf until a person reads the failure. The cost
  of (a) is a second kind of detached process launched from the wake path, bounded and
  short-lived. In both, `--recover` can be run by hand, and `sleeper list` shows the
  interrupted adoption, its phase and how to recover it. Until a recovery runs, a frozen
  session hears nothing.
  (iii) **No phase marker, but the session has already exited** (a person ended it
  after `create` was killed). Nothing `create` started is left to undo, and the
  conversation is complete. *Recommendation:* commit, as in the marker branch, so the
  agent becomes a sleeper (this is shape A), first killing the recorded Monitor if it is
  still there and stopped. The alternative, abort, deletes the record
  and leaves a stale row whose mail queues with nothing to wake it.
  (iv) **An old phase marker with the session still alive.** If `create` died between
  the marker write and its first SIGTERM, the session was never signalled, and the
  recovery may run hours later, on the next mail, while a person is using the pane (the
  pilots run in the owner's own tmux panes). The recovery always sends SIGTERM first
  with the full 15 s grace. The open part is whether it re-checks idleness first when
  the marker is older than 15 s plus the escalation. *Recommendation:* re-run step 4's
  idle and background-work checks (no transcript write since the marker's time); if the
  session is busy, run the abort path instead of ending it (the session was almost
  certainly never signalled, since Claude Code exits on SIGTERM, Test 8). The
  alternative is to end it regardless, which is simpler but can cut off a person's turn
  long after the session was judged idle.

## Out-of-scope (for now)

- Persistence across machine reboots
- Authentication / authorization between agents (single-user assumption)
- Streaming / long-form message bodies (current model is single-shot JSON messages)
