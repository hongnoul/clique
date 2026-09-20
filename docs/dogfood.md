# Dogfooding the gx10 clique server — graceful rollout plan

Server: `http://100.83.233.124:7777` over Tailscale.
GPU engine: vLLM FP8 `nemotron-3-nano-fp8` on :8000, 340+ TPS aggregate at 32-way batching,
16 `parallel_slots` on the clique node.

## Live status (2026-09-20, verified end-to-end from this Mac)

- Server tree deployed to gx10 `~/tcj` from this branch (tarball, backup kept
  as `~/tcj.bak-*`). Live at `http://100.83.233.124:7777` with the new routes:
  `/` menu, `/join.sh`, `/app.tgz`, `/repo.bundle`, `/v1/clique` now reports
  `protocol_version` + `server_sha` (`unknown` on gx10: tarball install, no git).
- Supervision: the system units need sudo. User-level units installed at
  `~/.config/systemd/user/clique-server.service` + `clique-node.service`
  (`Restart=always`, enabled). Server is `active` under the user manager.
- Verified: `clique onboard --dry` prints `server ok (protocol=1)`;
  echo-node join + submit + ledger increment (succeeded 47 -> 48).
- **Blocked money loop:** vLLM is down. `asus` is not in the `docker` group
  (`docker.sock` is `root:docker`), so the node unit waits in `start-pre` on
  `:8000/v1/models` forever. Fix on gx10 (needs one sudo):
  `sudo usermod -aG docker asus` then re-login, or `sudo systemctl start vllm`
  if a system unit exists. Until then gx10 serves onboarding + echo tasks only,
  no GPU throughput.
- **Update: money loop RECOVERED (02:00 ET).** `gx10-vllm` is `ready`
  (nemotron-3-nano-fp8, 16 slots), `:8000/v1/models` answers, and a live
  `--model nemotron` submit completed in 7.6s on node `7c1e3619`
  (succeeded 60+). Someone restarted vLLM. Dogfood waves 1+ are unblocked.

## Graceful principles

1. **Never break the golden path.** gx10's systemd stack (vllm + clique-server +
   clique-node) is the money loop. Teammate nodes are additive capacity only.
2. **Warn, never block, on version drift.** `/v1/clique` now exposes
   `protocol_version` + `server_sha`; `clique onboard` warns on mismatch and joins
   anyway. Hard failure is reserved for unreachable server.
3. **Echo first, GPU second.** Every teammate joins with `--runtime echo` first
   (proves networking + auth + routing in 60s), then upgrades to a real backend.
4. **One variable per wave.** Add one node class at a time so regressions attribute.

## Wave plan

| Wave | Who | Runtime | What it proves |
|---|---|---|---|
| 0 | Justin (this Mac) | echo -> ollama | onboard flow itself, version warnings |
| 1 | 1 teammate laptop | echo only | LAN/tailscale reachability, auth, dashboard shows node |
| 2 | same laptop | ollama qwen2.5-coder:7b | real inference routing, mixed-model cluster |
| 3 | Arch/NVIDIA box | vLLM or ollama + `--parallel-slots 8` | batching path beyond gx10 |
| 4 | rest of fleet | per-machine best | sustained goodput, ledger accrual |

Each wave gates on: node visible in `clique nodes`, 1 task succeeds end-to-end,
`clique stats` ledger increments, server log shows no errors.

## Onboarding flow (per machine)

```bash
# 0. tailscale up + reachable?
curl -s http://100.83.233.124:7777/   # should print the join menu

# 1. install (no GitHub, no PAT, no ssh key)
export CLIQUE_SERVER=http://100.83.233.124:7777
curl -fsSL $CLIQUE_SERVER/join.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# 2. one step: buttons, no scripts
clique            # Host Join Chat Dashboard (Join probes server,
                  # detects runtime, warns on drift, joins)
```

Wave 1 (echo, always works): press **Join**. Wave 2+ (real backend):
Join auto-detects ollama :11434 or llama-server :8080.

Headless or explicit (same paths the buttons call):

```bash
# echo first (proves networking + auth + routing in 60s)
clique join --server $CLIQUE_SERVER --runtime echo --param-b 7
# graceful variant: probe + detect + warn, then join in foreground
clique onboard --server $CLIQUE_SERVER --dry   # check first
clique onboard --server $CLIQUE_SERVER

# explicit backends:
# ollama mac:
clique join --server $CLIQUE_SERVER --runtime openai-compat \
  --base-url http://127.0.0.1:11434/v1 --model-name qwen2.5-coder:7b --param-b 7
# vllm box (batching):
clique join --server $CLIQUE_SERVER --runtime openai-compat \
  --base-url http://127.0.0.1:8000/v1 --model-name nemotron-3-nano-fp8 \
  --param-b 30 --parallel-slots 16

# verify from anywhere
clique nodes --server $CLIQUE_SERVER
clique submit "write a fizzbuzz in rust" --server $CLIQUE_SERVER
clique stats --server $CLIQUE_SERVER
```

Headless boxes Justin controls: `clique join-remote <ssh-host> --server $CLIQUE_SERVER`
runs the same `join.sh` over hidden ssh transport.

## Git migration: GitHub -> server-local

Goal: onboarding and dev need zero GitHub credentials. GitHub becomes a mirror,
not a dependency.

- **Install path (done):** `join.sh` + `/app.tgz` serve the running server tree.
  No clone, no PAT. `/repo.bundle` now serves the full history as a git bundle.
- **Teammate dev checkout (no GitHub account needed):**
  ```bash
  curl -s $CLIQUE_SERVER/repo.bundle -o /tmp/tcj.bundle
  git clone /tmp/tcj.bundle ~/tcj && cd ~/tcj
  git remote add origin-clock $CLIQUE_SERVER/repo.bundle  # refresh via re-download
  ```
- **Going forward:** treat the gx10 checkout (or a pinned tag) as the source of
  truth for dogfood. Push to GitHub when convenient for backup/publicity, but
  never require it in the join or update path. `clique onboard`'s SHA warning
  is the drift detector until GitHub is fully out of the loop.
- **Not yet done:** `bootstrap.sh` still documents the PAT flow for raw clones;
  point it at `/repo.bundle` or delete once no machine uses it. Server-side
  `state-repo`/`code-repo` (dulwich) already version without GitHub.

## Rollback / safety

- Teammate node misbehaves: `clique kick <node-id>` (op-gated) or just kill the agent;
  router retries in-flight tasks on survivors.
- Bad server deploy: systemd `clique-server.service` restarts; tokens are in-memory
  so agents re-register automatically (SDK does silent 401 retry).
- Version storm: mismatched nodes warn but keep serving echo tasks; upgrade them
  with a fresh `curl .../join.sh | sh` (bundle is always the server's own tree).
