# Live workspaces — realtime git collab

Two layers: memory is truth for seconds, git is truth for hours.

## Orchestrated workflow

```bash
# 1. human creates a shared workspace
clique workspace --create --files '{"main.py": "x = 1\n"}'
# -> w-abc123 seq=1

# 2. collaborators join live over websocket (patch in, delta out)
# ws://server/ws/workspace/w-abc123?token=<node-token>
# messages: workspace.patch -> workspace.delta, workspace.sync -> workspace.state

# 3. submit tasks linked to live files (agents see current content)
clique submit "summarize main.py" --workspace w-abc123
clique code-submit -p "add f" -f main.py=./main.py --workspace w-abc123 --tools

# 4. observe
clique workspace --show w-abc123            # snapshot versions
clique workspace --history w-abc123         # op log with rebase flags
clique workspace --commits w-abc123         # git checkpoints
clique workspace --flush w-abc123           # force checkpoint now
# /dash shows Live workspaces card; /ws/events streams workspace.patched
```

## Semantics

- Server is the sequencer: one lock per workspace, concurrent patches
  become linear history with global `seq` per workspace.
- Clients send `base_version`; stale writers are rebased (line shift),
  never rejected. Gap in `seq` -> send `workspace.sync` for full state.
- File patches are reliable; presence/cursor is droppable.
- Agents: task stamps head `seq` at submit; CODE_EDIT prompts embed
  live file text; mid-task moves arrive as `workspace.invalidate` and
  surface as re-read notes in the tool loop.
- Durability: debounced git commit (2s idle / 50 ops), force flush on
  task result, 30s tick safety net, flush on shutdown, rehydrate on boot.
- Guards: no `..` or absolute paths, no binaries, 1MB file cap,
  100 ops per patch, token required for WS writes.
