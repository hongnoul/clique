# Dogfooding the gx10 clique server — graceful rollout plan

Server: `http://100.83.233.124:7777` over Tailscale.
GPU engine: vLLM FP8 `nemotron-3-nano-fp8` on :8000, 340+ TPS aggregate at 32-way batching,
16 `parallel_slots` on the clique node.

## Live status (2026-09-20 03:40 ET, verified end-to-end from this Mac)

- gx10 `~/tcj` is now a **real git checkout** of this branch (bundle clone,
  prior tree kept as `~/tcj.bak-*`), so `/v1/clique` reports a true
  `server_sha` (bb67fca). Venv rebuilt in place; note venv shebangs break
  if the tree directory is moved, rebuild `.venv` after any rename.
- Supervision: user units `clique-server` + `clique-node` + **new
  `clique-tunnel`** (`Restart=always`, enabled). All three `active`.
- **Zero-context onboarding is live.** `clique-tunnel.service` runs a
  cloudflared quick tunnel (no account, no sudo) and writes the https URL
  to `~/.clique/public_url`; the server advertises it as `public_url` in
  `/v1/clique` and on `/`. A joiner needs no tailscale, no VPN, no LAN.
  The URL **rotates on tunnel restart**: always fetch the current one from
  `curl http://100.83.233.124:7777/` (or `/v1/clique`).
- Verified twice from pristine `$HOME`s over the public URL (including
  once after a deliberate tunnel restart with a rotated URL): install
  ~10s, Join -> node `ready`, `--model nemotron` submit answered in
  1.4-1.5s on `7c1e3619` through the tunnel, `clique leave` deregisters.
- Fail-fast: documented one-liners use `--connect-timeout 5`, so hitting
  a tailnet IP from off-tailnet errors in 5s instead of hanging.
- Money loop healthy: `gx10-vllm` ready (nemotron-3-nano-fp8, 16 slots),
  ledger 75+ succeeded.

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
# 0. reachable? (tailnet URL needs tailscale; the public URL needs nothing)
curl -s --connect-timeout 5 http://100.83.233.124:7777/   # prints the join menu + public_url

# 1. install (no GitHub, no PAT, no ssh key, no tailscale if using public_url)
export CLIQUE_SERVER=http://100.83.233.124:7777   # or the trycloudflare public_url from step 0
curl -fsSL --connect-timeout 5 $CLIQUE_SERVER/join.sh | sh
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
