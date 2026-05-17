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
      "last_seen": "2026-05-17T15:00:00Z"
    }
  ]
}
```

Rewritten atomically on every change. Stale entries (PID dead OR `last_seen` older than threshold) get pruned on read.

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

## Trust model

See [`architecture.md`](architecture.md#trust-model) for the full discussion. Briefly:

- Monitor descriptions and SessionStart context are **trusted policy** (controlled by the plugin author).
- Monitor stdout signals and message bodies are **untrusted data** (could originate from any agent).
- The bus is a *transport*; it does not impart trust to the contents it carries.
