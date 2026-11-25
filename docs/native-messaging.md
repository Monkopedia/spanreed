# Spanreed and Claude Code's native messaging

Claude Code can message between its own sessions natively. This page is the full
comparison: what that covers, where it stops, and one hazard to know before migrating
a fleet to it. The [README](../README.md#why-spanreed-when-claude-code-messages-natively)
has the short version.

Since **Claude Code v2.1.224** (v2.1.234 on native Windows; v2.1.248 for
same-machine messaging on Bedrock / Agent Platform / Foundry or with
feature-flag fetching off), sessions can message each other with no plugin,
no MCP server, and no registry: `ListAgents` finds your other sessions and
`SendMessage` delivers text to one by name, over a per-session socket. An idle
session is woken with the message; a busy one reads it between tool calls.
See [Message your other Claude Code sessions](https://code.claude.com/docs/en/cross-session-messaging).

**Same-machine only, on most setups.** Native cross-*machine* messaging routes
through Anthropic servers and requires
[Remote Control](https://code.claude.com/docs/en/remote-control) connected at
*both* ends — which needs a claude.ai sign-in on a paid plan, and is
unavailable with an API key, on Bedrock / Vertex / Foundry, or with
`ANTHROPIC_BASE_URL` pointed anywhere but `api.anthropic.com`. On Team and
Enterprise it is **off until an Owner enables it**, and an organization can
disable it outright. Where that applies, native messaging stops at the machine
boundary and [`spanreed conjoin`](../README.md#cross-host-experimental) — an SSH bridge
that never touches Anthropic servers — is the only cross-host option here.

**That is spanreed's core — discovery, delivery, and waking an idle peer — and
it was the hardest part to build.** If that is all you came here for, use the
native feature. It needs no install and no MCP tool surface, and it ships with
inbound controls (`crossSessionInbound`), per-sender rate limiting, and loop
protection that this project does not have. (It is not free: `ListAgents` and
`SendMessage` are always-on built-ins, and a delivered message counts toward
usage like a prompt you typed.)

**What it does not do**, and why this repo still exists:

- **Non-Claude agents.** Native messaging addresses Claude Code sessions only.
  A [Codex worker](architecture.md#codex-workers) has no socket and no row
  in `ListAgents`; spanreed gives it a bus address, a mailbox, and a wake.
- **Threading.** Native messages are plain text with no correlation id. Spanreed
  carries `msg_id` / `in_reply_to` and a blocking `wait_for_reply`, which is
  what request/response between agents is built on.
- **Sleepers.** A [sleeper](../README.md#sleepers-experimental)'s Claude exits
  when it is idle, freeing its memory, and stays on the bus by name; the next
  message resumes the same conversation in tmux. Native messaging has nothing that
  brings an exited conversation back when mail arrives.
- **Durable mail, on one machine.** Native same-machine delivery needs a live peer
  bound to a socket, so nothing holds mail for a session that does not exist yet.
  Spanreed inboxes are files, so mail to a local agent waits while it isn't
  running, including a sleeper that is asleep. Across machines the comparison runs
  the other way: a native cross-machine message to a peer listed `offline` *is*
  queued until its machine reconnects, but while a `conjoin` bridge is down the far
  host's agents drop off spanreed's bus and sends to them usually fail
  ([#34](https://github.com/Monkopedia/spanreed/issues/34)). That holds after a
  clean teardown; a killed `conjoin` can leave the far host's rows behind, so a send
  to the exact address still queues.
- **Fleet state.** `focus`, `status`, and the [activity log](architecture.md#activity-log-presence-history)
  — a pull-based read on what every agent is doing and a history of it. Native's
  closest primitive is a one-shot idle notice.
- **Cross-host on a managed account**, per the paragraph above.

**One migration hazard worth knowing before you try it.** With no
`crossSessionInbound` value set, Claude Code decides per message from the two
sessions' permission modes, sorting every session into one of two classes:
bypassing permission prompts, or prompting — where `auto`, `acceptEdits`, and
`dontAsk` all count as *prompting*. A message is **held** behind an approval
dialog exactly when the two classes differ, in *both* directions, and the
dialog drops the message after five minutes by default (`dialogExpiry`). A
fleet that is uniformly one class is fine; a **mixed** fleet stops talking to
whichever sessions are the odd one out. The sending Claude is told when a
same-machine message is held — though a `claude -p` sender needs v2.1.271 or
later to get that notice, and a `-p` receiver can't show the dialog at all, so
it holds for the deadline and then drops. Setting `crossSessionInbound:
accept` is the fix, and it is worth setting deliberately rather than
discovering.

**Not yet measured here:** whether native messaging holds up under this project's
own traffic pattern — bursts of multi-kilobyte review reports between ~20 agents —
given its per-sender rate limit and 50-message queue cap. Until that is tested,
treat the overlap above as read from Anthropic's documentation rather than
demonstrated. If you are choosing between them, test before you migrate.
