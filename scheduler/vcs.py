"""Local git version control over the shared server DB — implemented.

Vision dump: "Local .git version control system in the shared db of
server (consider open source framework, unless no adequate)".

Uses git via dulwich (pure-python, no libgit2/CLI dependency). The
server exports its mutable state (registry nodes, permissions, cron
defs, sessions) as canonical JSON files into a repo working tree at
``<data_dir>/state-repo`` and commits on change batches. sqlite remains
the runtime source of truth; the repo is the auditable history and
rollback vehicle.
"""

from __future__ import annotations

import difflib
import logging
from datetime import datetime, timezone
from pathlib import Path

from dulwich import porcelain
from dulwich.repo import Repo

log = logging.getLogger("clique.vcs")


class VcsService:
    """Snapshot/history/diff/rollback over exported server state.

    ``exporters`` maps a repo-relative filename to a zero-arg callable
    returning that file's canonical text content.
    """

    def __init__(self, repo_dir: Path | str) -> None:
        self.repo_dir = Path(repo_dir)
        self._exporters: dict[str, callable] = {}
        self._importers: dict[str, callable] = {}
        self._repo: Repo | None = None

    def register(self, filename: str, exporter, importer=None) -> None:
        """Register a state file. ``exporter() -> str`` produces content;
        optional ``importer(text)`` restores it on rollback."""
        self._exporters[filename] = exporter
        if importer is not None:
            self._importers[filename] = importer

    # -- lifecycle ------------------------------------------------------------

    def init(self) -> None:
        """Create the repo on first run and take a baseline commit."""
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        if (self.repo_dir / ".git").exists():
            self._repo = Repo(str(self.repo_dir))
        else:
            self._repo = porcelain.init(str(self.repo_dir))
            self.snapshot("baseline", actor="server")

    def _export_tree(self) -> list[str]:
        changed = []
        for name, exporter in self._exporters.items():
            path = self.repo_dir / name
            try:
                content = exporter()
            except Exception as e:  # never let one exporter break snapshots
                log.warning("exporter %s failed: %s", name, e)
                continue
            if not path.exists() or path.read_text() != content:
                path.write_text(content)
                changed.append(name)
        return changed

    # -- operations -----------------------------------------------------------

    def snapshot(self, message: str, actor: str) -> str | None:
        """Export current state and commit. Returns the commit sha, or
        None when nothing changed (no-op commits are skipped)."""
        assert self._repo is not None, "call init() first"
        changed = self._export_tree()
        first = not self._has_head()
        if not changed and not first:
            return None
        porcelain.add(self._repo, [str(self.repo_dir / n)
                                   for n in self._exporters])
        sha = porcelain.commit(
            self._repo, message=message.encode(),
            author=f"{actor} <{actor}@clique>".encode(),
            committer=b"clique-server <server@clique>")
        return sha.decode() if isinstance(sha, bytes) else str(sha)

    def _has_head(self) -> bool:
        try:
            self._repo.head()
            return True
        except KeyError:
            return False

    def history(self, limit: int = 50) -> list[dict]:
        """Commit log entries: sha, actor, message, timestamp."""
        assert self._repo is not None
        if not self._has_head():
            return []
        out = []
        walker = self._repo.get_walker(max_entries=limit)
        for entry in walker:
            c = entry.commit
            out.append({
                "sha": c.id.decode(),
                "actor": c.author.decode().split(" <")[0],
                "message": c.message.decode().strip(),
                "timestamp": datetime.fromtimestamp(
                    c.author_time, tz=timezone.utc).isoformat(),
            })
        return out

    def _tree_files(self, sha: str) -> dict[str, str]:
        commit = self._repo[sha.encode()]
        tree = self._repo[commit.tree]
        files = {}
        for name, _mode, blob_sha in tree.iteritems():
            files[name.decode()] = self._repo[blob_sha].data.decode()
        return files

    def diff(self, sha_a: str, sha_b: str) -> str:
        """Unified diff between two snapshots, for the audit UI."""
        assert self._repo is not None
        a_files = self._tree_files(sha_a)
        b_files = self._tree_files(sha_b)
        chunks = []
        for name in sorted(set(a_files) | set(b_files)):
            a = a_files.get(name, "").splitlines(keepends=True)
            b = b_files.get(name, "").splitlines(keepends=True)
            if a == b:
                continue
            chunks.extend(difflib.unified_diff(
                a, b, fromfile=f"a/{name}", tofile=f"b/{name}"))
        return "".join(chunks)

    def rollback(self, sha: str, actor: str) -> str:
        """Restore state from the snapshot at sha via registered
        importers, then commit the rollback as a new commit (history is
        never rewritten). Returns the new commit sha."""
        assert self._repo is not None
        files = self._tree_files(sha)
        for name, content in files.items():
            (self.repo_dir / name).write_text(content)
            importer = self._importers.get(name)
            if importer is not None:
                importer(content)
        porcelain.add(self._repo, [str(self.repo_dir / n) for n in files])
        new_sha = porcelain.commit(
            self._repo, message=f"rollback to {sha[:12]}".encode(),
            author=f"{actor} <{actor}@clique>".encode(),
            committer=b"clique-server <server@clique>")
        return new_sha.decode() if isinstance(new_sha, bytes) else str(new_sha)
