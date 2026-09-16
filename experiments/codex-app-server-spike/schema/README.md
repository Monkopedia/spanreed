# Vendored app-server schemas

`ClientRequest.json` and `ServerRequest.json` from `codex app-server
generate-json-schema --out DIR` on codex-cli **0.154.0**, macOS 26.6.0.

Two of 610 files, kept because they are the authoritative answer to questions
this project kept guessing at, and `codex` is not installed on the Linux host
where most of the work happens:

- **`ServerRequest.json`** — every request app-server can send *to* the client.
  All ten must be answered; an unanswered one hangs the server with no error and
  no timeout.
- **`ClientRequest.json`** — every request the client can send, including the
  `AskForApproval` and `SandboxPolicy` definitions that `--mode` maps onto.

`sandbox_policy()` was first written as a guess — `{"mode": "workspace-write"}`
where the schema says `{"type": "workspaceWrite"}`. Wrong key and wrong value,
and app-server may simply ignore a policy it does not recognise, which leaves a
worker unconfined while the approval log says it is sandboxed. These files exist
so that is checked rather than guessed.

**They are a snapshot, not a contract.** Re-dump on a version bump; if they
disagree with a running server, the server is right.
