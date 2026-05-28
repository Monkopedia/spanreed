# Cross-host (`spanreed conjoin`)

How to bridge two hosts' Spanreed buses over a persistent SSH pipe. The design lives in [`architecture.md`](architecture.md#cross-host-the-ssh-bus-bridge); this doc is the runnable setup guide.

> **Experimental.** Point-to-point only (no multi-hop / mesh), no peer discovery, supervision (start-on-boot, restart-on-crash) is your job. `conjoin` reconnects on its own when the pipe drops, but it doesn't reconnect *itself* — it's a foreground command.

## Prerequisites

- `spanreed-bus` ≥ 0.0.4 installed on **both** hosts (`uv tool install spanreed-bus`, or `upgrade` if already installed).
- **Key-based, non-interactive SSH** from the initiating host to the peer. `conjoin` reconnects unattended, so it can't answer a password prompt.

## Step 1 — verify non-interactive SSH

This is the single load-bearing check. From the host you'll run `conjoin` from:

```bash
ssh -o BatchMode=yes <peer-host> echo ok
```

- Prints `ok` → SSH is ready, skip to [Step 3](#step-3--start-the-bridge).
- Prompts for a password or prints `Permission denied (publickey,password)` → SSH needs configuring. Continue to [Step 2](#step-2--fix-non-interactive-ssh-auth).

## Step 2 — fix non-interactive SSH auth

The two cases this hits in practice:

### Case A: a non-default key name

Plain `ssh` only auto-offers default identity files (`~/.ssh/id_rsa`, `id_ecdsa`, `id_ed25519`). A key named e.g. `~/.ssh/id_ed25519_monkopedia` won't be offered, even if the matching public key is already in the peer's `authorized_keys`. `ssh-copy-id` *will* find such a key (it scans for them), so its output saying "all keys were skipped because they already exist" is a clue — the key is installed; ssh just isn't using it.

Fix: add a block to `~/.ssh/config`:

```sshconfig
Host <peer-host>
    User <peer-user>
    IdentityFile ~/.ssh/<your-key>
    IdentitiesOnly yes
    AddKeysToAgent yes
    UseKeychain yes        # macOS only — stores passphrase in the keychain
```

Re-test Step 1.

### Case B: a passphrase-locked key, no agent loaded

Test by passing the key explicitly:

```bash
ssh -o BatchMode=yes -i ~/.ssh/<your-key> <peer-host> echo ok
```

If that fails non-interactively but works interactively (with a prompt), the key is passphrase-locked and there's no usable agent. Load it once:

```bash
ssh-add --apple-use-keychain ~/.ssh/<your-key>   # macOS
ssh-add ~/.ssh/<your-key>                        # Linux
```

The `~/.ssh/config` block above also ensures the key gets re-added to the agent on first use after reboot.

## Step 3 — start the bridge

In a spare terminal (it runs in the foreground until you Ctrl-C it):

```bash
spanreed conjoin <peer-host>
```

It probes the peer for the `spanreed` absolute path (via an interactive-shell `command -v` over SSH — this works around the stripped non-interactive PATH), opens the SSH pipe, and starts mirroring agents in both directions.

If it exits immediately, see [Troubleshooting](#troubleshooting).

## Step 4 — confirm the bridge is healthy

From another terminal on the same host:

```bash
spanreed list | grep '@<peer-host>'
```

You should see the peer's live agents qualified as `agent-xxxx@<peer-host>`. Equivalently on the peer, the local-host's agents show up as `agent-xxxx@<your-host>`.

Send a cross-host message:

```bash
spanreed send --from <a-local-agent-id> --to <some-id>@<peer-host> --body "ping across"
```

The receiving session wakes on it as if it were a local message.

## Updating `spanreed` on a peer

Plugin reloads only re-read the local cache — they do **not** fetch from GitHub. To pull new commits onto a peer (and apply them):

```bash
ssh <peer-host> 'claude plugin marketplace update spanreed && claude plugin update spanreed@spanreed'
```

Then restart any Claude session on that host so its MCP server picks up the new code (`/reload-plugins` is not enough — it doesn't restart MCP server processes).

Also bump the Python package on the peer, since `spanreed-mcp` and the `spanreed` CLI come from there:

```bash
ssh <peer-host> 'zsh -lc "uv tool upgrade spanreed-bus"'
```

(The `zsh -lc` is to source the user's profile so `uv` is on PATH; non-interactive SSH otherwise gets a stripped `$PATH`.)

Don't `git pull` the plugin cache dirs directly — Claude tracks `gitCommitSha` in `~/.claude/plugins/installed_plugins.json`, and bypassing the CLI leaves that state stale.

## Troubleshooting

- **`conjoin` exits immediately after starting.** Almost always one of:
  - The peer is on an older `spanreed-bus` without the `conjoin` command (need ≥ 0.0.4 — see Updating above).
  - The peer's non-interactive `$PATH` doesn't include `spanreed`, and the interactive-shell probe failed too. Test manually: `ssh <peer> 'zsh -ic "command -v spanreed"'` should print an absolute path. If it doesn't, you can override the probe with `--remote-spanreed /abs/path/to/spanreed`.
- **Permission denied at SSH.** Re-run the Step 1 verify. If it still fails, the `BatchMode=yes` flag is unmasking that your interactive shell silently uses a key the non-interactive path doesn't — that's Step 2.
- **`spanreed list` doesn't show `@peer` agents.** Wait a few seconds for the first registry sync (default 3s). If still empty, the peer may have no live local agents — check `ssh <peer> spanreed list` directly.
- **Agents are `@peer` but the bridge process died.** Mirrored entries are owned by the bridge's PID; if the bridge dies for good they go stale and drop out on the next `list_agents`. Restart `conjoin`.
