"""REST API of the server node (FastAPI route specs).

All routes require a node session token except /join assets. Op-gated
routes call PermissionManager.check first.

Route summary:

Nodes & clique
- GET  /v1/clique                 -> clique name, server info, policy
- GET  /v1/nodes                  -> list NodeInfo (dashboard devices view)
- GET  /v1/nodes/{node_id}        -> one node detail
- POST /v1/nodes/{node_id}/kick   -> op: remove a node
- GET  /v1/clusters               -> clusters with membership

Tasks
- POST /v1/tasks                  -> submit TaskRequest (idempotency key required)
- GET  /v1/tasks/{task_id}        -> state + result metadata
- POST /v1/tasks/{task_id}/cancel -> request cancel, returns effective outcome
- GET  /v1/tasks?state=queued     -> queue listing

Sessions
- POST /v1/sessions               -> create session
- GET  /v1/sessions               -> active sessions (dashboard)
- GET  /v1/sessions/{id}          -> detail incl. pinned node, version
- POST /v1/sessions/{id}/migrate  -> op or owner: rare migration
- DELETE /v1/sessions/{id}        -> close

Permissions
- GET  /v1/permissions            -> levels of all nodes
- POST /v1/permissions/op         -> /op {target}
- POST /v1/permissions/deop       -> /deop {target}
- POST /v1/permissions/policy     -> set policy mode
- GET  /v1/permissions/audit      -> audit log

Cron
- POST /v1/cron                   -> request job
- GET  /v1/cron                   -> list (pending + approved)
- POST /v1/cron/{id}/approve      -> op approval
- POST /v1/cron/{id}/reject       -> op rejection
- POST /v1/cron/{id}/disable      -> requester or op

Suggestions & stats
- GET  /v1/suggestions            -> active suggestions
- POST /v1/suggestions/{id}/dismiss
- GET  /v1/stats                  -> queue stats, per-node throughput
- GET  /v1/vcs/history            -> state snapshot log
- GET  /v1/vcs/diff?a=&b=         -> snapshot diff
- POST /v1/vcs/rollback           -> op: restore snapshot

Compatibility
- POST /v1/chat/completions       -> OpenAI-compatible adapter: wraps a
                                     TaskRequest (+ optional session) so
                                     existing tools can point at the
                                     clique as a provider.
Join assets
- GET  /join                      -> join page (QR + one-liner)
- GET  /join.sh                   -> installer script
- GET  /models/{artifact}         -> LAN GGUF mirror (avoid WAN pulls)
"""

from __future__ import annotations


def create_app(server: "SchedulerServer") -> "FastAPI":
    """Build the FastAPI app with the routes above, wired to the
    server's subsystems. Auth middleware validates node tokens and
    attaches node identity + OpLevel to the request context."""
    ...
