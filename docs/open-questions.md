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
- **Two bridges sharing one host's peer record** (from #122, recommendation: accept two-way, refuse a second `connect`). `peers/<host>.json` is keyed only by host and both ends of a bridge write it, so two live bridges can share it: by design when `conjoin` runs in both directions (this host's `connect` and the `serve` the peer's `conjoin` started here), and by accident when two `conjoin`s to one host run. The record describes whichever wrote it last. The resolver's #122 refusal applies only to a row owned by the bridge the record names, so a row the other live bridge mirrored still resolves. What remains: when the record's bridge stops, the send verdict, which judges by the record, says `NOT DELIVERED: NO BRIDGE IS RUNNING` although the other bridge forwards the mail, until that bridge next writes the record (within a sync tick while it is attached; up to its reconnect backoff, about 45 s, while it is reconnecting). `list_peers` and `spanreed list` read the same record and say the same. Recommendation: accept this for the two-way pair; have `conjoin` refuse to start only when the record's live, not-stopping owner has `role: connect`, which leaves two-way `conjoin` working.

## Remote sleeper management (#86, designed, not built)

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

- **Default-on after a few releases?** Deferred by the owner on 2026-11-25: "that sounds fine for
  now, maybe after a few releases we turn it on by default". Revisit once remote management has
  shipped and been used. Turning it on by default would also turn on what "What turning it on
  grants" describes, for every user who runs `conjoin`.

## Out-of-scope (for now)

- Persistence across machine reboots
- Authentication / authorization between agents (single-user assumption)
- Streaming / long-form message bodies (current model is single-shot JSON messages)
