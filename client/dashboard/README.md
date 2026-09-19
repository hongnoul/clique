# Dashboard specs

Two frontends over the same API (`scheduler/api/rest.py` + `/ws/events`):

1. **TUI** (`client/tui.py`, textual): for monitor-less nodes over SSH (GX10).
2. **Web** (this directory): static HTML + htmx served by the server node at `/dash`.

## Views

### Devices
- All nodes: name, cluster/model, status badge, measured tok/s, memory, battery, current task.
- Join panel: QR + one-liner installer, default model Qwen 2.5 or bring-your-own (HF repo ref). Future: auto-detect best model for the hardware.
- Per-node actions (owner/op): pause, drain, swap model, kick (op).

### Sessions
- Active sessions with pinned node, owner, turn count, context version.
- Click-through live token stream (`/ws/sessions/{id}`). Future: multi-user simultaneous viewing.

### Queue and load
- Queue depth and wait time per task type and per cluster.
- Overload heatmap (task type x cluster) from `SuggestionEngine.overload_report`.
- Suggestion cards ("node-3 could serve a code-gen model") with dismiss and a copyable `clique model swap` command.

### Governance
- Permission table with /op and /deop buttons (op-gated).
- Cron requests pending approval with approve/reject.
- VCS snapshot log with diff viewer and op-gated rollback.

## Files (to be created at implementation time)

| File | Purpose |
|---|---|
| `index.html` | Shell + nav, htmx polling/websocket extensions |
| `devices.html` | Devices view fragment |
| `sessions.html` | Sessions view fragment |
| `queue.html` | Queue/suggestions fragment |
| `governance.html` | Permissions/cron/vcs fragment |

No build step. Upgrade to Preact only if fragment complexity demands it.
