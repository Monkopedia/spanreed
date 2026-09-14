# Codex app-server spike

**One question:** can a second process start a turn in a Codex session someone
else has open?

If yes, spanreed can treat a Codex session as a real peer — mail arrives, the
monitor calls `turn/start`, the agent wakes with the human watching. If no, a
Codex agent is a mailbox read at hook boundaries, and the integration is worth
much less. Everything else about the design follows from this answer.

## Run it

On a machine with Codex signed in:

```sh
python3 spike.py                     # steps 1-3, read-only
python3 spike.py --turn <thread-id>  # all four; starts ONE short turn
```

Stdlib only, installs nothing, touches no repo. The sole side effect is one turn
in a thread you nominate, with a prompt that asks for a fixed string and forbids
tools and file access.

**Steps 1-3 passing does not answer the question.** They show you can connect and
look. `--turn` is the test.

## Why it is written this way

It was written on a machine with no Codex, against documentation that could not
be executed — and against an interface OpenAI's own docs call *"experimental and
not supported for production workloads"*. So every step is an assumption, and the
script's real job is to tell you **which** assumption broke:

```
A1  `codex app-server --listen unix://PATH` is the right invocation
A2  the unix transport frames messages as newline-delimited JSON
A3  `initialize` is required first, per connection
A4  method names are thread/list, thread/loaded/list, turn/start
A5  turn/start takes a threadId and an input message
```

A failure naming A1–A5 means **this script guessed wrong**. A clean protocol
error from the server means **Codex declines to do it**. Those are different
answers and the output keeps them apart.

Every step prints PASS / FAIL / SKIP / DID NOT RUN with a reason. A step that
could not run says so rather than printing nothing — "no threads found" when you
never connected is worse than a crash.

## What was verified before shipping it

The Codex side could not be. The script's own logic was, against a stub
app-server:

| case | result |
|---|---|
| no `codex` on PATH | step 1 FAIL, steps 2-4 `DID NOT RUN`, exit 1 |
| full run, steps 1-3 | PASS, step 4 SKIP with the caveat, exit 1 |
| `turn/start` accepted | step 4 PASS, exit 0 |
| `turn/start` refused by server | step 4 FAIL naming the server's error, exit 1 |

The stub interleaves a notification and a response to a different id before each
real reply, so the client's id-filtering is exercised rather than assumed.

## Reporting back

The verdict table is the output. Worth capturing separately if it happens:
**a turn that runs but the human never sees** — that would mean turns are
possible but invisible, which is a different answer from either yes or no.
