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
spanreed list
```

The `PEERS` section is the whole health check. A working bridge looks like:

```
PEERS — cross-host bridges (1 recorded)
  ATTACHED  adolin  (this end is the 'connect' side)
         bridge pid:    4242
         attached at:   2026-09-16T12:00:00+00:00 (1h 3m ago)
         last frame:    1s ago
         registry sync: 2026-09-16T13:02:58+00:00 (2s ago), 4 agent(s), 1208 sync(s) total
                        'adolin' held 5 local row(s) then, 1 judged stale there
```

and the `AGENTS` section lists the peer's agents qualified as `agent-xxxx@<peer-host>`. Equivalently on the peer, this host's agents show up as `agent-xxxx@<your-host>`.

**Read `registry sync` before anything else.** It is the line that separates a bridge that carries mail from one that also lets you address the far side; the two are independent, and only this line distinguishes them. Every unhealthy state it can show is in [Troubleshooting](#troubleshooting).

Send a cross-host message:

```bash
spanreed send --from <a-local-agent-id> --to <some-id>@<peer-host> --body "ping across"
```

The receiving session wakes on it as if it were a local message. `spanreed send` exits 0 only when a live session (or an attached bridge) is watching the recipient's inbox; exit **3** means the message was written but is only queued, and the reason is on stderr.

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
- **`spanreed list` doesn't show `@peer` agents.** Do not guess, and in particular do not conclude the agent is gone — `list_agents` cannot see a peer's agents for *any* of the reasons below, so an empty list distinguishes none of them. Read the `PEERS` section instead; it names exactly one of five states:

  | what `PEERS` shows | what it means | fix |
  |---|---|---|
  | `(none)` | no bridge was ever started here | `spanreed conjoin <peer>` |
  | `DETACHED <host>` | the `conjoin` process that owned it is gone | restart `spanreed conjoin <peer>` |
  | `ATTACHED`, `registry sync: NEVER` | the pipe is alive and carrying frames, but the peer has never advertised its agents. **Messages still cross in both directions** — this is why nothing else looks wrong | run `spanreed list` **on the peer**: if it shows live local agents, the two hosts' `spanreed` versions likely disagree (upgrade both); check the `note:` line for a dropped frame |
  | `ATTACHED`, `ZERO AGENTS ADVERTISED` | the peer answered and has nothing live to offer. The `held N local row(s), M judged stale` line says whether its agents exist but are failing its own PID liveness check | fix it on the peer — its sessions are not registered, or their recorded pids are dead |
  | `ATTACHED`, a recent sync with N agents | the bridge is healthy. An id you cannot address is genuinely unknown | check the ids the peer is advertising in the `AGENTS` section |

  The first sync normally lands within a few seconds (default cadence 3s).

- **Agents are `@peer` but the bridge process died.** Mirrored entries are owned by the bridge's PID; if the bridge dies for good they go stale and drop out on the next `list_agents`. The peer record survives, showing `DETACHED`, which is how you tell this apart from a bridge that was never started. Restart `conjoin`.
- **A `note:` line on a peer.** The bridge records what it could not process there: a frame it could not model (usually a version skew — upgrade both ends), a registry frame that arrived before the handshake (self-healing), or non-JSON noise on the pipe (a login banner or a shell profile that prints on non-interactive `ssh`, which will also break the handshake).
