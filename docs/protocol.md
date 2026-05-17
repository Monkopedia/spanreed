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

- `register_agent(name, working_dir, pid, agent_id?) -> Agent` — upsert by id if supplied.
- `deregister_agent(agent_id) -> {ok: true}`
- `list_agents(include_stale=false) -> [Agent, ...]`
- `send_message(from_agent, to_agent, body, in_reply_to?) -> Message`
- `recv_messages(agent_id, since_msg_id?) -> [Message, ...]`
- `wait_for_reply(agent_id, in_reply_to, timeout_s) -> Message | null` — blocks up to `timeout_s`.

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
