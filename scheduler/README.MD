# scheduler/ — server-node subsystems

Runs on exactly one node per clique: registry (`registry.py`), router
(`router.py`), shared contexts (`context_store.py`), sessions (`sessions.py`),
/op permissions (`permissions.py`), op-approved cron (`cron.py`), git-versioned
state DB (`vcs.py`), overload suggestions (`suggestions.py`), and the REST/WS
API (`api/`). Behavior contracts follow ../ARCHITECTURE.MD; see ../SPEC.md.
