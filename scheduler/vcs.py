"""Local git version control over the shared server DB.

Vision dump: "Local .git version control system in the shared db of
server (consider open source framework, unless no adequate)".

Decision: use git itself via dulwich (pure-python, no libgit2/CLI
dependency). No adequate non-git OSS alternative offers the same
snapshot/diff/rollback semantics plus universal tooling.

Mechanics: the server exports its mutable state (contexts, registry,
permissions, cron defs, config) as canonical text/JSON files into a
repo working tree at ``<data_dir>/state-repo`` and commits on change
batches. sqlite remains the runtime source of truth; the repo is the
auditable history and rollback vehicle.
"""

from __future__ import annotations

from pathlib import Path


class VcsService:
    def __init__(self, repo_dir: Path, db: "Database") -> None: ...

    async def init(self) -> None:
        """Create the repo on first run and take a baseline commit."""
        ...

    async def snapshot(self, message: str, actor: str) -> str:
        """Export current DB state to the working tree and commit.
        Returns the commit sha. No-op commit is skipped. Called on
        privileged changes (op grants, policy, cron approval) and on a
        timer for context state."""
        ...

    async def history(self, path_filter: str | None = None, limit: int = 50) -> list[dict]:
        """Commit log entries: sha, actor, message, timestamp, files."""
        ...

    async def diff(self, sha_a: str, sha_b: str) -> str:
        """Unified diff between two snapshots, for the audit UI."""
        ...

    async def rollback(self, sha: str, actor: str) -> None:
        """Op-gated (permissions "manage_vcs"): restore DB state from the
        snapshot at sha, then commit the rollback as a new commit
        (history is never rewritten)."""
        ...
