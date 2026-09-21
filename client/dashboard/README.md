# Dashboard specs

Two frontends over the same API (`scheduler/api/rest.py` + `/ws/events`):

1. **TUI** (`client/tui.py`, textual): for monitor-less nodes over SSH (GX10).
2. **Web** (this directory): static HTML served by the server node at
   `/dash` (live view) and `/chat` (ask it something).

## Views

### Devices
- All nodes: name, cluster/model, status badge, measured tok/s, memory, battery, current task.
- Join panel: QR + one-liner installer, default model Qwen 2.5 or bring-your-own (HF repo ref). Future: auto-detect best model for the hardware.
- Per-node actions: pause, drain, swap model, kick.

### Sessions
- Active sessions with pinned node, owner, turn count, context version.
- Each id links into `/chat`, which opens that transcript.

### Queue and load
- Queue depth and wait time per task type and per cluster.
- Click a running task to mirror its tokens (`/ws/tasks/{id}`) in the live output card.
- Overload heatmap (task type x cluster) from `SuggestionEngine.overload_report`.
- Suggestion cards ("node-3 could serve a code-gen model") with dismiss and a rejoin with the suggested model.

### Chat
`clique submit` in a browser, with sessions as the chat list:

- Sidebar of sessions (title = first user turn), newest first; `+ new`
  defers session creation until the first message, like the sticky CLI
  session does.
- A turn is `POST /v1/tasks` with the session id, streamed off
  `/ws/tasks/{id}` (polling `partial_output` if the socket drops) and
  footed with the node, wall time, and token count the CLI prints.
- `/ws/sessions/{id}` mirrors turns taken by other clients (the CLI,
  another tab) into the open transcript, and compactions show as a rule
  in the log.
- Routing: the model picker is the CLI's `--model` (cluster key prefix,
  hard filter); `auto` lets the router choose.

## Files

| File | Purpose |
|---|---|
| `index.html` | Live dashboard: nodes, sessions, queue, leaderboard, live output |
| `chat.html` | Chat page: session sidebar, streamed transcript, composer |
| `assets/base.css` | Theme tokens + shared chrome (cards, tables, header, menu) |
| `assets/chrome.js` | Theme toggle, menu, inlined logo/icons, fetch + ws helpers |
| `assets/mascot.js` | EyeQ mascot emotions (dashboard only) |

No build step. Upgrade to Preact only if page complexity demands it.
