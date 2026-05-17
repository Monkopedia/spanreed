# Cross-session experiment

Two real Claude Code sessions exchanging messages through a shared filesystem-based bus. Validates the design end-to-end before committing to the MCP server build.

## Layout

```
spanreed-alice/     plugin identifying its session as agent 'alice'
spanreed-bob/       plugin identifying its session as agent 'bob'
```

Each plugin's Monitor runs `tail -n 0 -F /tmp/spanreed-test/<agent>-inbox.txt`. New lines appended to the inbox surface as notifications. The trust framing (description = trusted policy, inbox content = untrusted data) carries forward from the `bus-test` experiment.

## Running the test

Two terminals, ideally side by side.

**Reset state** (do this between runs):

```bash
rm -rf /tmp/spanreed-test
```

**Terminal 1 — alice:**

```bash
claude --plugin-dir /home/jmonk/git/spanreed/experiments/cross-session/spanreed-alice
```

**Terminal 2 — bob:**

```bash
claude --plugin-dir /home/jmonk/git/spanreed/experiments/cross-session/spanreed-bob
```

**Kick it off in alice's terminal**, e.g.:

> Send a message to bob asking what version of TypeScript his repo is using.

Watch what happens in bob's terminal: she should wake on the new inbox line, recognize the request, formulate an answer (or flag needs-user-attention), and append her reply to alice's inbox. Then alice's terminal should wake and surface bob's response.

## Watch for

- **Round-trip works**: alice sends → bob receives + replies → alice receives reply
- **JSON shape preserved** across the round-trip
- **No re-processing of stale messages** on session restart (`-n 0` flag should prevent it)
- **Autonomous vs. needs-user-attention disposition** picks the right path
- **UX with two terminals**: anything weird about both showing "1 monitor running"

## Known weirdness

- Inbox files are at `/tmp/spanreed-test/` so they vanish on reboot. Fine for a test.
- Both agents writing to the same file simultaneously is theoretically a race; in practice with two agents it's effectively never an issue for single-line JSON appends.
- Outbound from the user-initiated side ("alice, send a message to bob") tests Claude's ability to *originate* bus messages, not just reply.
