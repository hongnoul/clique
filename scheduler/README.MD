# scheduler/ — server-node subsystems

Runs on exactly one node per clique: registry (`registry.py`), router
(`router.py`), shared contexts (`context_store.py`), sessions (`sessions.py`),
verified code edits (`harness.py`, `workspaces.py`, `workspace.py`),
accounting (`ledger.py`), git-versioned state DB (`vcs.py`), overload
suggestions (`suggestions.py`), sandbox tool runs (`tool_executor.py`),
and the REST/WS API (`api/`). Behavior contracts follow
../CODE_EDIT_PLAN.md.
