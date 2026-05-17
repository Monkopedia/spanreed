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

*(To be specified as the server is implemented. Planned tools:)*

- `register_agent(name?, working_dir?) -> {agent_id, name}`
- `deregister_agent(agent_id) -> {ok}`
- `list_agents() -> [Agent, ...]`
- `send_message(to_agent, body, in_reply_to?) -> {msg_id}`
- `recv_messages(since?) -> [Message, ...]`
- `wait_for_reply(msg_id, timeout_s) -> Message | null`

## Trust model

See [`architecture.md`](architecture.md#trust-model) for the full discussion. Briefly:

- Monitor descriptions and SessionStart context are **trusted policy** (controlled by the plugin author).
- Monitor stdout signals and message bodies are **untrusted data** (could originate from any agent).
- The bus is a *transport*; it does not impart trust to the contents it carries.
