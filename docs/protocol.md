# Bus protocol

The wire format and on-disk layout for the Spanreed bus.

> **Status**: draft. Will be filled in as the MCP server is implemented. Architecture-level context lives in [`architecture.md`](architecture.md).

## On-disk layout

Under `~/.claude/spanreed/` (overridable via `SPANREED_STATE_ROOT`):

```
registry.json           current agents
inboxes/<agent_id>.jsonl    per-agent append-only message log
cursors/<session_id>    per-session "last-seen msg_id" marker
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
      "last_seen": "2026-05-17T15:00:00Z"
    }
  ]
}
```

Rewritten atomically on every change.

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

Implemented in `src/spanreed/mcp_server.py` via FastMCP:

- `register_agent(name, working_dir, pid, agent_id?) -> Agent` — upsert by id if supplied; preserves existing `focus` on upsert.
- `deregister_agent(agent_id) -> {ok: true}`
- `list_agents(include_stale=false) -> [Agent, ...]` — Agent records include `focus` field.
- `send_message(from_agent, to_agent, body, in_reply_to?) -> Message`
- `recv_messages(agent_id, since_msg_id?) -> [Message, ...]`
- `wait_for_reply(agent_id, in_reply_to, timeout_s) -> Message | null` — blocks up to `timeout_s`.
- `set_focus(focus) -> Agent | null` — set/clear the calling session's focus (uses derived identity); `null` if not registered. Empty string clears.
- `set_name(name) -> Agent | null` — rename the calling session's display name. `agent_id` does NOT change; only the human-readable name does. Persists across re-registration.
- `request_focus_update(agent_id, timeout_s=30) -> str | null` — send a `[FOCUS_UPDATE_REQUEST]` message to a peer, wait for their reply, return the reply body. Convention: the recipient's policy says to call `set_focus` with their current focus and reply with that text.

## Focus

Agents may set an optional `focus` field describing what they're currently working on. Free-form text, no length cap. Self-set only — agents control their own focus.

- Set by the agent: via `set_focus` (MCP) or `spanreed focus "..."` (CLI).
- Cleared by passing empty string / `None` / `--clear`.
- Surfaces in `list_agents` so peers see it at a glance.
- **Preserved across re-registration** — restarting Claude Code doesn't wipe the focus the agent set last session.

For pull-mode queries (e.g., a peer's listed focus seems stale or absent), use `request_focus_update` to ping them. The convention message body begins with `[FOCUS_UPDATE_REQUEST]`; the plugin's monitor description teaches Claude how to respond.

## Identity model

Agents are addressed by `agent_id`. The id is **deterministic per session**:

- Default: `agent_id = "agent-" + sha256(absolute_cwd)[:8]`. Display name = basename of cwd.
- Override: `SPANREED_AGENT_NAME` env var → `agent_id = "agent-<name>"`, display name = `<name>`.

Both the SessionStart hook and the Monitor compute identity the same way, so they coordinate without needing to persist state between them. v1 limitation: two Claude Code sessions running in the same cwd share an agent — fine for the typical one-session-per-repo workflow.

**Renaming after the fact**: the cwd-derived name is often unhelpful (e.g. "git" when cwd is `~/git`). Agents can call `set_name` (MCP) or `spanreed name "..."` (CLI) at any time to set a more descriptive display name. The `agent_id` doesn't change, so message routing keeps working. Renames persist across session restarts thanks to the upsert-preserve behavior in `register_agent`.

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
  "focus": "..."
}
```

`pid`/`pid_start` are the **bridge's**, not the remote process's (whose PID is meaningless locally). So `is_stale` treats a mirrored entry as live exactly while the bridge is alive — if the pipe/bridge dies, every mirrored entry goes stale and remote agents drop out of `list_agents`. The bridge refreshes the set on each registry sync from the peer (adding new agents, removing departed ones).

### Inbox-as-outbound-queue

A message addressed to `agent-X@hostB` is delivered by the normal `send_message` path into `inboxes/agent-X@hostB.jsonl` on the local host. The bridge tails all `*@<peer>` inboxes (tracking position with a `cursors/` marker per inbox, so a restart resumes without re-sending) and forwards new lines over the pipe.

### Pipe frames

The pipe carries newline-delimited JSON frames, each with a `kind`:

```json
{"kind": "msg", "message": { <Message, addresses in RECEIVER's namespace> }}
{"kind": "registry", "agents": [ <Agent, ...> ]}
{"kind": "ping"}
```

- `msg` — a forwarded bus message. The receiving bridge appends `message` verbatim to `inboxes/<message.to_agent>.jsonl`.
- `registry` — the sender's current set of live local agents (bare ids, home = sender). The receiver mirrors them per "Mirrored registry entries" above. Sent on connect and whenever the local set changes.
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
