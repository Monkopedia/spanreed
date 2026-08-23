# Bus protocol

The wire format and on-disk layout for the Spanreed bus.

> **Status**: draft. Will be filled in as the MCP server is implemented. Architecture-level context lives in [`architecture.md`](architecture.md).

## On-disk layout

Under `~/.claude/spanreed/` (overridable via `SPANREED_STATE_ROOT`):

```
registry.json           current agents
inboxes/<agent_id>.jsonl    per-agent append-only message log
cursors/<session_id>    per-session "last-seen msg_id" marker
config.json             bus-wide flags (status_tracking, activity_log)
activity-log.jsonl      append-only focus/status transition log (opt-in)
```

### `registry.json`

```json
{
  "agents": [
    {
      "agent_id": "...",
      "name": "...",
      "working_dir": "/path",
      "pid": 12345,
      "pid_start": 8675309,
      "last_seen": "2026-05-17T15:00:00Z",
      "focus": "...",
      "status": "working"
    }
  ]
}
```

Rewritten atomically on every change. `focus` and `status` are omitted/`null` when unset.

**Liveness / staleness.** An agent is *present* iff its `pid` is alive **and** that PID's start-time still matches the `pid_start` captured at registration. Stale entries (PID dead, or PID recycled onto an unrelated process so the start-time no longer matches) get filtered on read and pruned by `prune_stale`. `pid_start` is the process start-time (Linux: clock ticks since boot, from `/proc/<pid>/stat` field 22); it's the guard against PID reuse. When it can't be read (no `/proc`, e.g. macOS) it is `null`, and liveness falls back to a bare PID-alive check.

There is **no `last_seen` TTL**: agents do not heartbeat on a timer (wasteful wakeups), so a live-but-quiet agent is never flagged stale. `last_seen` is retained as informational metadata (when the agent last registered/renewed), not as a liveness signal.

### `inboxes/<agent_id>.jsonl`

One JSON message per line, append-only. Schema matches the `Message` Pydantic model in `src/spanreed/protocol.py`:

```json
{"msg_id": "...", "from_agent": "...", "to_agent": "...", "body": "...", "ts": "...", "in_reply_to": null}
```

JSON keys match Python field names exactly (no aliasing) — `from` is reserved in Python, and the asymmetry of `from`/`to` ↔ `from_agent`/`to_agent` was an unnecessary footgun.

### `cursors/<session_id>`

Plain text file containing the last-delivered `msg_id` for this session. Used on session restart to replay any messages that arrived while the session was down.

## MCP tool surface

Implemented in `src/spanreed/mcp_server.py` via the `mcp` SDK's `MCPServer` (`mcp.server.mcpserver`; this was `FastMCP` in `mcp` 1.x, removed in 2.0):

- `register_agent(name, working_dir, pid, agent_id?) -> Agent` — upsert by id if supplied; preserves existing `focus` on upsert.
- `deregister_agent(agent_id) -> {ok: true}`
- `list_agents(include_stale=false) -> [Agent, ...]` — Agent records include `focus` and `status` fields.
- `send_message(from_agent, to_agent, body, in_reply_to?) -> Message` — `to_agent` is **validated against the registry**: a known `agent_id` is used as-is, a unique display *name* resolves to its id, an ambiguous name or an unknown recipient **raises**. This stops a misaddressed message (e.g. sent to a display name) from being silently written to an inbox no monitor tails. Resolution uses the include-stale registry view, so a crashed-but-not-pruned agent stays addressable (mail waits for its restart).
- `recv_messages(agent_id, since_msg_id?) -> [Message, ...]`
- `wait_for_reply(agent_id, in_reply_to, timeout_s) -> Message | null` — blocks up to `timeout_s`.
- `set_focus(focus) -> Agent | null` — set/clear the calling session's focus (uses derived identity); `null` if not registered. Empty string clears.
- `set_status(status) -> Agent | null` — set the calling session's status (one of `idle | working | needs_input | blocked`); `null` if not registered. Pull-only (does not notify). Always functional regardless of the `status_tracking` flag.
- `set_name(name) -> Agent | null` — rename the calling session's display name. `agent_id` does NOT change; only the human-readable name does. Persists across re-registration.
- `request_focus_update(agent_id, timeout_s=30) -> str | null` — send a `[FOCUS_UPDATE_REQUEST]` message to a peer, wait for their reply, return the reply body. Convention: the recipient's policy says to call `set_focus` with their current focus and reply with that text.

## Focus

Agents may set an optional `focus` field describing what they're currently working on. Free-form text, no length cap. Self-set only — agents control their own focus.

- Set by the agent: via `set_focus` (MCP) or `spanreed focus "..."` (CLI).
- Cleared by passing empty string / `None` / `--clear`.
- Surfaces in `list_agents` so peers see it at a glance.
- **Preserved across re-registration** — restarting Claude Code doesn't wipe the focus the agent set last session.

For pull-mode queries (e.g., a peer's listed focus seems stale or absent), use `request_focus_update` to ping them. The convention message body begins with `[FOCUS_UPDATE_REQUEST]`; the plugin's monitor description teaches Claude how to respond.

## Status

Agents may report a `status` describing how much human attention they need — one of (ordered by escalating need):

| status | meaning | needs human? |
|---|---|---|
| `idle` | registered, not actively working | no |
| `working` | actively making progress | no |
| `needs_input` | wants a human decision/answer; may still proceed | soft |
| `blocked` | stopped; cannot continue without a human | hard |

"Who needs a human" is `status ∈ {needs_input, blocked}`. Properties:

- Set by the agent via `set_status` (MCP) or `spanreed status <level>` (CLI). Self-set only.
- **Pull, not push** — setting status writes the registry and surfaces in `list_agents`; it does **not** notify any peer.
- **Reset to `null` on re-registration** (unlike `focus`, which is preserved) — a fresh session isn't `blocked` just because the last one was; a stale status would mislead the fleet view. `null` = not reported.
- `idle` is **best-effort**: an agent stops running exactly when it goes idle, so it can't reliably report that transition (there is no Stop hook). The human-need levels (`needs_input`/`blocked`) are reliable because the agent declares them while active.

### `status_tracking` toggle

Status tracking is **off by default** and enabled bus-wide via `spanreed status-tracking on` (persisted in `<state_root>/config.json` as `{"status_tracking": true}`). The flag gates **only** whether `session-start` injects the status-maintenance instruction into agent context — so a bus with tracking off pays zero context tokens for the feature. `set_status` itself always works regardless of the flag; when off, agents simply aren't told to maintain status and `list_agents` shows `status: null`.

The instruction binds status to actions the agent already takes rather than relying on willpower: it directs the agent to (a) set `working`/`idle` as an explicit **first action on resume** (catching the resumed-mid-work case), (b) set `needs_input`/`blocked` at the same moment it escalates per disposition rule 4, and (c) set `idle` on completion. Auto-inferring status from bus activity was considered and rejected — status is a *semantic* declaration (only the agent knows `blocked` vs `working`), activity-inference can't produce `needs_input`/`blocked` and would mismark an active-but-quiet agent as idle, and every variant reintroduces the timer/wakeup model the liveness design rejected. The complementary lever is the urithiru workflow playbooks, which call `set_status` at their own task transitions.

## Activity log

An opt-in, append-only timeline of `focus` and `status` transitions, intended to
be dumped and summarized (e.g. piped into an LLM for a daily "what did my agents
do" digest). Spanreed only **emits** the log; summarization is the caller's job —
no LLM dependency lives in the bus.

- **Opt-in**, off by default: `spanreed activity-log on` (persisted in
  `config.json` as `{"activity_log": true}`). When off, nothing is written — zero
  cost, same model as `status_tracking`.
- When on, `set_focus` and `set_status` append one record **per genuine change**
  (a no-op re-set of the same value is not logged; clearing focus logs `value: null`).
- Records go to a single fleet-wide `activity-log.jsonl` under the state root —
  one file to pipe for a whole-fleet digest. One JSON object per line:

```json
{"ts":"2026-06-22T14:30:01Z","agent_id":"agent-a4d87503","name":"kodemirror","kind":"focus","value":"vim-scroll bug cluster"}
```

  `kind` is `focus` or `status`; `value` is the new focus text / status (`null`
  for a cleared focus). `name` is captured at write time so a digest reads
  "kodemirror did X" rather than a hex id. Schema = the `ActivityRecord` model in
  `src/spanreed/protocol.py`.

- Read with `spanreed log`, which emits matching records as JSON lines:
  `--since` takes a relative age (`24h`/`30m`/`7d`) or an ISO-8601 timestamp;
  `--agent` filters by `agent_id` **or** display name. Example digest pipeline:
  `spanreed log --since 24h | <llm summarize prompt>`.

Retention/rotation and cross-host replication (whether a conjoined peer's
transitions appear in the local log) are open — see
[`open-questions.md`](open-questions.md).

## Identity model

Agents are addressed by `agent_id`. An id is **minted** from the directory a session starts in, and thereafter **belongs to the session**, not to the directory.

### Minting (`session-start`, `register`)

- Default: `agent_id = "agent-" + sha256(absolute_cwd)[:8]`. Display name = basename of cwd.
- Override: `SPANREED_AGENT_NAME` env var → `agent_id = "agent-<name>"`, display name = `<name>`.

v1 limitation: two Claude Code sessions started in the same cwd share an agent — fine for the typical one-session-per-repo workflow.

### Resolving (every other command)

Re-deriving from the cwd is only correct while the session stands where it started. A session that `cd`s — into a subdirectory, a sibling repo, or a git worktree — derives a **different** id, and every id it derives is a real, reachable address, so nothing errors:

- If the drifted-to id is unregistered, replies raise on the sender's side and the conversation ends without either party learning why.
- If it is registered but **stale**, replies are accepted and written to an inbox no monitor tails.
- If it is registered and **live**, replies are delivered to a **different agent**. This is not hypothetical: on a bus with an agent rooted at `~/git`, any peer that `cd`s to `~/git` derives that agent's id, and the reply to its message lands in that agent's inbox.

So identity is resolved against the session, in this order:

1. `SPANREED_AGENT_NAME`, if set — an explicit override outranks everything.
2. The registry entry whose `pid` is `$CLAUDE_PID`, if exactly one **live** entry matches. A session's *own* entry records that value (see "What `pid` means" below), which is what makes the lookup work. Not every writer does — `_ensure_registered` deliberately does not, mirrored `@peer` entries carry the bridge's pid, and any caller passing an explicit `pid` is outside the convention — so this is a lookup that usually succeeds, not an invariant the store guarantees.
3. Otherwise, the cwd derivation above. This is the path for a human running `spanreed` at a terminal, who has no session to belong to; it is also what a session gets before its hook has run.

**Stale entries are excluded from (2), and that exclusion is load-bearing.** `$CLAUDE_PID` is the caller's own ancestor, so it is alive by construction — which means the only staleness a *matching* entry can carry is a `pid_start` mismatch, i.e. precisely pid reuse. Admitting stale entries therefore buys nothing and costs everything: an abandoned agent whose pid the OS later recycled onto a live session would be adopted as that session's identity, and the drift warning below would assert it was correct. A session mid-restart is not a counter-example — its entry still holds the *old*, dead pid, so it cannot match the new one either way.

**Residual, and this spec should not soften it.** Where `pid_start` could not be read at registration (macOS has no `/proc`), `is_stale` falls back to a bare PID-alive check and accepts a small reuse risk — see its docstring. The *probability* of reuse is unchanged by anything here. The *consequence* is not: before identity was resolved this way, no code path turned a missing `pid_start` into a wrong `agent_id`. This resolver creates that path, and on macOS it creates it with the guard inert. That is introduced, not merely inherited. Tracked as issue #31; where the guard could not run, the drift warning says so.

Ambiguity is refused, not guessed: if two entries claim the pid, (2) is skipped.

When (2) fires and disagrees with (3), the command **prints the disagreement to stderr** and proceeds under the registered identity. Correcting silently would leave the agent still believing what `pwd` told it.

The MCP server needs none of this: it is a per-session process whose own cwd is fixed at spawn, so a `cd` inside the session cannot move it. The drift was only ever reachable through the CLI.

This is a change in kind, worth stating plainly: minting still needs no shared state — `derive_agent_identity` is a pure function of cwd and env, which is what let the hook and the Monitor agree without coordinating. **Resolving does read shared state**, because once a session can leave the directory it started in, its identity stops being a property of that directory and has to be looked up somewhere. The registry is that somewhere, and it already existed.

`CLAUDE_PID` is an undocumented Claude Code environment variable and not every spawned process receives it (MCP servers do not). Where it is absent, resolution degrades to the cwd answer — silently, and indistinguishably from a human at a terminal. The surfaces that need the anchor (the CLI, the Monitor) do have it today; if that ever stops being true, the drift returns with no signal.

**Renaming after the fact**: the cwd-derived name is often unhelpful (e.g. "git" when cwd is `~/git`). Agents can call `set_name` (MCP) or `spanreed name "..."` (CLI) at any time to set a more descriptive display name. The `agent_id` doesn't change, so message routing keeps working. Renames persist across session restarts thanks to the upsert-preserve behavior in `register_agent`.

### What `pid` means

`pid` is **the claude pid of the process whose liveness the entry tracks.** Settled by the owner on 2026-08-23 (issue #29), which also declined a `session_pid`/`pid` split and a separate remote marker as unnecessary once the field has a single meaning.

One *meaning*, not one mechanism: which pid satisfies it differs by writer, and one writer satisfies it by deliberately **not** reading the session anchor. Read the table as four answers to the same question, not as a rule with an exception bolted on.

| entry | records | because |
|---|---|---|
| a local session | its own `$CLAUDE_PID` | its liveness *is* the session's |
| a mirrored `@peer` | the **bridge's** pid | the remote pid is meaningless locally; the bridge's liveness is that entry's liveness (see "Mirrored registry entries") |
| a human at a terminal | `os.getppid()`, their shell | there is no anchor to consult, and the parent shell is the closest available proxy |
| an auto-registered stub (`_ensure_registered`) | `os.getppid()`, **always** | see below — this row is a deliberate exception, not an oversight |

`getppid()` **for a human at a terminal** is the least-wrong proxy in the absence of an anchor, not a definition. A script, `make` recipe, cron job or CI step invoking `spanreed` gets a parent that dies at end-of-line — structurally the same pathology as the Bash-tool case above. It is kept because it fails *closed*: such an entry vanishes on its own rather than asserting a liveness that is not there. Do not build on "the shell is the human".

**This is what the field *means*; it is not an invariant the store enforces.** `register_agent` — both the `StateStore` method and the MCP tool (`mcp_server.py`) — takes `pid` and `agent_id` from its caller and validates neither. The rules below are the **`spanreed` CLI's** conventions for choosing that value, and any caller passing an explicit `pid` (the MCP tool, the bridge, `--pid`) is outside them by design, because an explicit pid is the documented way to say "I know whose liveness this is." A reader must not infer from this section that a registry entry's `pid` has been checked. Tracked as its own gap: the MCP `register_agent` tool applies none of this and structurally cannot, since MCP server processes receive no `CLAUDE_PID`.

So the CLI's writers read `$CLAUDE_PID` and fall back to `getppid()` when it is unset — every writer except `_ensure_registered`, which never consults it at all (below). **`getppid()` is not a substitute for the anchor inside a session** — under a Bash tool the parent is an ephemeral `zsh`, alive for the length of one command, and an entry written that way is not a correct value that decays but *the wrong value at the moment of writing*. Its symptoms are that the agent silently disappears from `list_agents` while running normally, and that identity resolution falls back to the cwd with no warning, because a wrong pid is indistinguishable from an unregistered session.

**One writer nevertheless uses `getppid()` on purpose, and the reasoning inverts there.** `_ensure_registered` creates a stub when `focus`/`status`/`name` find no entry.

Enumerated by how it is reached, because this is the part every rewrite of this section has garbled — each row stands alone and none depends on the sentence next to it:

| way in | is the id this session's? | so which pid, and why |
|---|---|---|
| resolution fell back to the cwd | **no**, by construction — the anchor found nothing, so we are standing somewhere that is not our own entry | `getppid()`. Recording the anchor would attach our liveness to a foreign entry permanently: it would never decay, `is_stale` would confirm it, and resolution would then find two live entries claiming one pid. The ephemeral pid is right because it **fails closed** — the stub evaporates when the command does. |
| `SPANREED_AGENT_NAME` is set | **yes** — precedence 1 never consults the registry, so an override session reaches the stub for its own id with no fallback involved | `getppid()` today, and this is the case the exception is *not* written for. The consequence is that an override session never appears in `list_agents` and can never be found by the anchor. Pre-existing; nothing here changed it. Whether the stub should special-case the override is **#38**. |
| nothing registered at all (a human) | **n/a** — there is no session | `getppid()`, which for a human is the right answer rather than a fallback (above). |

The rule is not "prefer the anchor"; it is *record the pid of the process whose liveness this entry tracks* — and for a stub created for someone else, that is emphatically not us.

This applies to the SessionStart hook too, which is where it is least obvious. `hooks.json` declares `"type": "command"`; a shell running a **single simple command** `exec`s it in place rather than forking, which is why `getppid()` there returned the claude process at all. Adding `|| true` to that command line makes the shell fork, and every session from that moment registers a pid that dies with the hook — silently, with nothing failing. Measured in `sh`, `dash`, `bash` and `zsh`.

A redirection (`spanreed session-start 2>/dev/null`) forks **only under bash**; `dash` and `zsh` still exec in place. So how fragile the old behaviour was depended on which shell runs hooks — which this project neither controls nor has measured. **That uncertainty is the argument for the change, not against it:** an invariant that holds or fails depending on the host's `/bin/sh` is not one to build identity on. Reading `$CLAUDE_PID` removes the dependency; **do not reintroduce `getppid()` here on the grounds that it "works".**

**`spanreed register` refuses to default the pid in three cases**, exiting non-zero and writing nothing. `--pid` bypasses all of them, by design: it is how a caller says "I know whose liveness this is", and the command that repairs any of these states must not be blocked by the state it repairs.

| guard | refused when | because |
|---|---|---|
| **ours** | a live entry already holds `$CLAUDE_PID` under a different `agent_id` | defaulting would put our liveness on a second entry that is not ours |
| **theirs** | the target `agent_id` is already registered and live under another pid | defaulting would overwrite a running agent: they fall back to a cwd-derived id and we resolve as them |
| **ambiguous** | several live entries claim `$CLAUDE_PID` | the registry is already ambiguous about who owns it; adding another is not a repair |

*ours* and *theirs* are the same test applied to each operand, and guarding only *ours* left *theirs* open. Refer to these by name below, not by position — a sentence that says "the first two" is one inserted row away from meaning something else. From inside a session there is no correct default: `getppid()` is a shell that dies with the command, and `$CLAUDE_PID` is the *registrar's* process, which would stamp the registrar's liveness onto someone else's entry. Of those two defaulting strategies the anchor is the worse — it fails **open**, reading healthy indefinitely, with `is_stale` actively confirming it because pid-alive and `pid_start` both hold of the wrong process. A dead shell pid at least announces itself by vanishing. Refuse rather than guess, per the same rule the bridge applies to an invalid host label.

**`theirs` is not gated on the anchor**, so it fires for a human at a terminal too — overwriting a running agent is wrong whoever is typing. *ours* and *ambiguous* can only fire inside a session, since both are about `$CLAUDE_PID`.

**Live entries only, in all three.** An entry with a dead pid is #29's victim state rather than a running agent to protect, and a session repairing its own must not be blocked by the corruption it is repairing.

The *ours* guard is deliberately on the **anchor** and not on the resolver's answer. Asking "does this differ from the id I resolve to?" conflates *not mine* with *differs from the cwd answer*, and gets both directions wrong once an entry is corrupt: it refuses a session's own repair (#29's victim, whose entry holds a dead pid) while allowing a drifted `register` with no `--agent-id` to mint a fresh id under the live pid. An **empty** anchor — no live entry claims our pid — is positive evidence that nothing live is being taken over, which is what makes first registration and repair safe.

**`register_agent` upsert semantics**: when called with an already-registered `agent_id`, only `pid`, `pid_start`, and `last_seen` are updated; `name`, `working_dir`, and `focus` are preserved. This is what makes in-session customizations (set_name, set_focus) sticky across the SessionStart hook firing on every restart.

## CLI surface

The `spanreed` CLI wraps the same operations for shell/script use and is what the plugin's hook + monitor invoke:

| Command | Purpose |
|---|---|
| `spanreed agent-id` | Print this session's deterministic agent_id |
| `spanreed inbox-path AGENT` | Print the inbox file path for an id |
| `spanreed register [...]` | Upsert this session into the registry |
| `spanreed deregister AGENT` | Remove an agent |
| `spanreed list` | List registered agents (JSON) |
| `spanreed send --to AGENT --body BODY [--from F] [--in-reply-to ID]` | Post a message |
| `spanreed recv AGENT [--since ID]` | Dump an agent's inbox |
| `spanreed inbox-watch` | `tail -F` this session's inbox (plugin Monitor) |
| `spanreed session-start` | Register + emit SessionStart hook JSON (plugin hook) |
| `spanreed status [LEVEL]` | Set this session's status (`idle\|working\|needs_input\|blocked`), or show it |
| `spanreed status-tracking [on\|off]` | Enable/disable bus-wide status tracking, or show the setting |
| `spanreed activity-log [on\|off]` | Enable/disable bus-wide activity logging, or show the setting |
| `spanreed log [--since AGE] [--agent ID\|NAME]` | Dump the activity log as JSON lines |
| `spanreed conjoin HOST` | Bridge this bus to a peer's over a persistent SSH pipe (`--serve` is the remote plumbing end) |

## Cross-host bridge wire-format

The SSH bus-bridge (design rationale in [`architecture.md`](architecture.md)) connects two local buses over one persistent duplex pipe. This section specifies the on-the-wire details.

### Host-qualified agent ids

Global identity is `agent-X@homehost`. On its **home** host the agent keeps its bare `agent-X` id (unchanged — local traffic and the existing tools are untouched). On a **foreign** host it appears as `agent-X@home`. The `@` separator is not otherwise legal in a generated `agent_id` (those are `agent-<hex>`), so it unambiguously marks "remote, routed via the bridge." `@` is filesystem-safe, so `inboxes/agent-X@home.jsonl` is a normal inbox file.

### Mirrored registry entries

For each live agent on the peer, the bridge writes a registry entry on the local host:

```json
{
  "agent_id": "agent-X@hostB",
  "name": "...",
  "working_dir": "...",
  "pid": <bridge's own pid>,
  "pid_start": <bridge's own start-time>,
  "focus": "...",
  "status": "..."
}
```

Both self-set presence fields are carried through: `focus` and `status` are mirrored from the peer's entry as-is, so a remote agent's `needs_input`/`blocked` is visible to a local `list_agents` scan. They inherit the same bounded staleness as everything else in the snapshot — a change on the peer (including the `status` reset on re-registration) reaches the mirror on the next registry sync.

`pid`/`pid_start` are the **bridge's**, not the remote process's (whose PID is meaningless locally). So `is_stale` treats a mirrored entry as live exactly while the bridge is alive — if the pipe/bridge dies, every mirrored entry goes stale and remote agents drop out of `list_agents`. The bridge refreshes the set on each registry sync from the peer (adding new agents, removing departed ones).

### Inbox-as-outbound-queue

A message addressed to `agent-X@hostB` is delivered by the normal `send_message` path into `inboxes/agent-X@hostB.jsonl` on the local host. The bridge tails all `*@<peer>` inboxes (tracking position with a `cursors/` marker per inbox, so a restart resumes without re-sending) and forwards new lines over the pipe.

### Pipe frames

The pipe carries newline-delimited JSON frames, each with a `kind`:

```json
{"kind": "hello", "host": "<the sender's own host label>"}
{"kind": "msg", "message": { <Message, addresses in RECEIVER's namespace> }}
{"kind": "registry", "agents": [ <Agent, ...> ]}
{"kind": "ping"}
```

- `msg` — a forwarded bus message. The receiving bridge appends `message` verbatim to `inboxes/<message.to_agent>.jsonl`.
- `registry` — the sender's current set of live local agents (bare ids, home = sender). The receiver mirrors them per "Mirrored registry entries" above. Sent on connect and whenever the local set changes.
- `hello` — the sender's own host label, which becomes the `@host` suffix for every id it owns. Sent once, first, by both ends. **Validated on receipt**: alphanumeric at both ends, `.`/`-`/`_` inside, at most 253 characters. The receiver interpolates this value into a filesystem glob when selecting outbound inboxes, so a metacharacter would be a wildcard rather than a name.

  On a label that fails validation the receiver **sends nothing further and closes**. Specifically, it does **not** send a `registry` frame — that frame carries agent ids, display names, absolute working directories, pids and focus text, and a peer whose identity was just rejected must not receive it. `hello` is the only frame a refused peer ever sees, and it was already in flight. The refusal is reported on stderr, naming the rule and the `--label` override; stdout is the pipe and carries nothing but frames.
- `ping` — keepalive; lets each side detect a dead pipe and lets `connect` trigger reconnect.

### Address rewriting (done by the sending bridge)

The sender rewrites so the receiver can stay dumb (just append). For a message leaving host `H` toward peer `P`, read from `inboxes/<to>@P.jsonl`:

- `to_agent`: strip `@P` → bare `agent-X` (it is local on `P`).
- `from_agent`: if bare (`agent-Y`, home `H`) → qualify to `agent-Y@H`; if already `agent-Z@Q` (a relayed third party) → leave as-is (but note multi-hop is out of scope for v1, so in practice `from` is always a local agent being qualified).
- `in_reply_to` is an opaque msg_id and is never rewritten.

The inverse holds on receipt: addresses arrive already in the local namespace, so the receiving bridge appends without translation.

## Trust model

See [`architecture.md`](architecture.md#trust-model) for the full discussion. Briefly:

- Monitor descriptions and SessionStart context are **trusted policy** (controlled by the plugin author).
- Monitor stdout signals and message bodies are **untrusted data** (could originate from any agent).
- The bus is a *transport*; it does not impart trust to the contents it carries.
