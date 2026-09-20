"""Live workspace service — two-layer real-time collab.

Layer 1 (live, in-memory): file buffers + per-file versions + global seq.
All async writers go through one asyncio.Lock per workspace. The server
is the sequencer: concurrent patches become linear history.

Layer 2 (durable, git): debounced dulwich commits. Memory is truth for
seconds, git is truth for hours.

Patch model (line-based, code-oriented, not full OT):
  ops = [{"op": "insert", "line": 12, "text": "...\\n"},
         {"op": "delete", "line": 20, "count": 2}]

Clients send base_version per file. Fast path when base == head.
Stale path rebases line numbers over ops applied since base.
Presence/cursor is droppable; file patches are never dropped.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("clique.workspace")


@dataclass
class FileState:
    text: str = ""
    version: int = 0
    # op log since creation: list of (seq, ops) for rebase
    history: list[dict] = field(default_factory=list)


@dataclass
class Workspace:
    workspace_id: str
    repo_dir: Path
    files: dict[str, FileState] = field(default_factory=dict)
    seq: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    dirty: bool = False
    last_flush: float = 0.0
    subscribers: set[str] = field(default_factory=set)  # node_ids / viewers
    # pending commit coalescing
    _flush_task: asyncio.Task | None = None


class WorkspaceService:
    """Manager of live workspaces with debounced git checkpoints."""

    FLUSH_IDLE_S = 2.0
    FLUSH_MAX_OPS = 50

    def __init__(self, base_dir: Path | str, commit_fn=None) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._workspaces: dict[str, Workspace] = {}
        self._commit_fn = commit_fn  # optional hook: (ws_id, seq_range, actors) -> sha
        self._ops_since_flush: dict[str, int] = {}
        self._actors_since_flush: dict[str, set[str]] = {}
        self.rehydrate()

    @staticmethod
    def _check_path(path: str) -> None:
        if not path or path.startswith("/") or ".." in Path(path).parts:
            raise ValueError(f"bad path: {path!r}")

    def rehydrate(self) -> list[str]:
        """Reload workspaces that have a git repo on disk (server restart).

        Reads head file contents from each repo dir, assigns version 1
        (history starts fresh; git holds the deep past). Returns ids.
        """
        found = []
        for child in sorted(self.base_dir.iterdir()):
            if not child.is_dir() or not (child / ".git").exists():
                continue
            wid = child.name
            if wid in self._workspaces:
                continue
            ws = Workspace(workspace_id=wid, repo_dir=child)
            try:
                from dulwich.repo import Repo
                Repo(str(child))  # validate
            except Exception:
                continue
            for fp in sorted(child.rglob("*")):
                if ".git" in fp.parts or not fp.is_file():
                    continue
                if fp.name == ".clique-ws":
                    continue
                try:
                    rel = str(fp.relative_to(child))
                    ws.files[rel] = FileState(text=fp.read_text(), version=1)
                except (UnicodeDecodeError, OSError):
                    continue  # skip binaries/unreadables
            ws.seq = 1
            self._workspaces[wid] = ws
            found.append(wid)
        if found:
            log.info("rehydrated %d live workspaces: %s", len(found), found)
        return found

    async def shutdown_flush(self) -> dict[str, str | None]:
        """Flush all dirty workspaces (call on server shutdown)."""
        out = {}
        for wid in list(self._workspaces):
            try:
                out[wid] = await self.force_flush(wid)
            except Exception as e:
                log.warning("shutdown flush %s failed: %s", wid, e)
                out[wid] = None
        return out

    # -- lifecycle ------------------------------------------------------

    def create(self, workspace_id: str | None = None,
               initial_files: dict[str, str] | None = None) -> Workspace:
        wid = workspace_id or f"w-{uuid.uuid4().hex[:12]}"
        if not wid or "/" in wid or ".." in wid:
            raise ValueError(f"bad workspace id: {wid!r}")
        repo_dir = self.base_dir / wid
        repo_dir.mkdir(parents=True, exist_ok=True)
        ws = Workspace(workspace_id=wid, repo_dir=repo_dir)
        for path, text in (initial_files or {}).items():
            self._check_path(path)
            if "\x00" in text:
                raise ValueError(f"binary rejected: {path}")
            if len(text) > 1_000_000:
                raise ValueError(f"file too large: {path}")
            ws.files[path] = FileState(text=text, version=1)
            (repo_dir / path).parent.mkdir(parents=True, exist_ok=True)
            (repo_dir / path).write_text(text)
        ws.seq = 1
        self._workspaces[wid] = ws
        self._init_git(ws)
        return ws

    def get(self, workspace_id: str) -> Workspace:
        try:
            return self._workspaces[workspace_id]
        except KeyError:
            raise KeyError(f"no such workspace {workspace_id}")

    def list_ids(self) -> list[str]:
        return list(self._workspaces)

    def _init_git(self, ws: Workspace) -> None:
        try:
            from dulwich import porcelain
            from dulwich.repo import Repo
            if not (ws.repo_dir / ".git").exists():
                porcelain.init(str(ws.repo_dir))
            # ensure baseline commit so history() works
            repo = Repo(str(ws.repo_dir))
            try:
                repo.head()
            except KeyError:
                (ws.repo_dir / ".clique-ws").write_text(ws.workspace_id)
                porcelain.add(repo, [str(ws.repo_dir / ".clique-ws")])
                porcelain.commit(
                    repo, message=b"workspace init",
                    author=b"clique-server <server@clique>",
                    committer=b"clique-server <server@clique>")
        except Exception as e:
            log.warning("workspace git init failed %s: %s", ws.workspace_id, e)

    # -- live patch path -------------------------------------------------

    async def apply_patch(self, workspace_id: str, path: str,
                          base_version: int, ops: list[dict],
                          actor: str) -> dict:
        """Apply one client's patch. Returns broadcast event.

        Serialized per workspace. Rebases stale patches onto head.
        """
        ws = self.get(workspace_id)
        self._check_path(path)
        if len(ops) > 100:
            raise ValueError("too many ops in one patch (max 100)")
        async with ws.lock:
            fst = ws.files.get(path)
            if fst is None:
                fst = ws.files[path] = FileState()
            rebased = False
            applied_ops = ops
            if base_version != fst.version:
                applied_ops = self._rebase_ops(fst, base_version, ops)
                rebased = True
            new_text = self._apply_ops(fst.text, applied_ops)
            fst.text = new_text
            fst.version += 1
            ws.seq += 1
            fst.history.append({"seq": ws.seq, "version": fst.version,
                                "ops": applied_ops,
                                "actor": actor, "base": base_version,
                                "rebased": rebased})
            # trim history to last 200 per file
            if len(fst.history) > 200:
                fst.history = fst.history[-200:]
            ws.dirty = True
            self._ops_since_flush[workspace_id] = \
                self._ops_since_flush.get(workspace_id, 0) + 1
            self._actors_since_flush.setdefault(workspace_id, set()).add(actor)
            event = {
                "workspace_id": workspace_id, "path": path,
                "version": fst.version, "seq": ws.seq,
                "ops": applied_ops, "actor": actor, "rebased": rebased,
            }
            # trigger debounced flush
            if (self._ops_since_flush.get(workspace_id, 0) >= self.FLUSH_MAX_OPS):
                asyncio.create_task(self._flush(ws))
            elif ws._flush_task is None or ws._flush_task.done():
                ws._flush_task = asyncio.create_task(self._debounced_flush(ws))
            return event

    def _apply_ops(self, text: str, ops: list[dict]) -> str:
        lines = text.splitlines(keepends=True)
        # normalize: ensure each line ends with \n except maybe last
        for op in ops:
            kind = op.get("op")
            line = int(op.get("line", 1))  # 1-indexed
            idx = max(0, min(line - 1, len(lines)))
            if kind == "insert":
                ins = op.get("text", "")
                if ins and not ins.endswith("\n"):
                    ins += "\n"
                lines[idx:idx] = ins.splitlines(keepends=True)
            elif kind == "delete":
                count = int(op.get("count", 1))
                del lines[idx:idx + count]
            elif kind == "replace_file":
                return op.get("text", "")
            else:
                raise ValueError(f"unknown patch op {kind!r}")
        return "".join(lines)

    def _rebase_ops(self, fst: FileState, base_version: int,
                    ops: list[dict]) -> list[dict]:
        """Shift line numbers over ops applied after base_version.

        Each history entry records the version it produced. Entries with
        version > base_version are newer than what the client saw.
        """
        newer = [h for h in fst.history
                 if h.get("version", 0) > base_version]
        out = [dict(o) for o in ops]
        for h in newer:
            for hop in h["ops"]:
                hop_kind = hop.get("op")
                if hop_kind == "replace_file":
                    # can't rebase line ops over full replace: keep ops
                    # best-effort (broadcast will correct clients on next sync)
                    continue
                h_line = int(hop.get("line", 1))
                h_count = int(hop.get("count", 1)) if hop_kind == "delete" \
                    else len(str(hop.get("text", "")).splitlines() or [""])
                for o in out:
                    if o.get("op") == "replace_file":
                        continue
                    o_line = int(o.get("line", 1))
                    if hop_kind == "insert" and o_line >= h_line:
                        o["line"] = o_line + h_count
                    elif hop_kind == "delete" and o_line > h_line:
                        o["line"] = max(h_line, o_line - h_count)
        return out

    # -- reads ------------------------------------------------------------

    def snapshot(self, workspace_id: str) -> dict:
        ws = self.get(workspace_id)
        return {
            "workspace_id": workspace_id, "seq": ws.seq,
            "files": {p: {"version": f.version, "text": f.text}
                      for p, f in ws.files.items()},
        }

    def file_state(self, workspace_id: str, path: str) -> dict:
        ws = self.get(workspace_id)
        fst = ws.files.get(path, FileState())
        return {"workspace_id": workspace_id, "path": path,
                "version": fst.version, "seq": ws.seq, "text": fst.text}

    def history(self, workspace_id: str, path: str | None = None,
                limit: int = 50) -> list[dict]:
        """Recent applied ops for audit/dashboard. Newest last."""
        ws = self.get(workspace_id)
        out = []
        paths = [path] if path else sorted(ws.files)
        for p in paths:
            fst = ws.files.get(p)
            if fst is None:
                continue
            for h in fst.history[-limit:]:
                out.append({"path": p, "seq": h["seq"],
                            "version": h["version"], "actor": h["actor"],
                            "rebased": h["rebased"], "ops": h["ops"]})
        out.sort(key=lambda h: h["seq"])
        return out[-limit:]

    def git_history(self, workspace_id: str, limit: int = 20) -> list[dict]:
        """Durable checkpoint log from the workspace git repo."""
        ws = self.get(workspace_id)
        try:
            from dulwich.repo import Repo
            repo = Repo(str(ws.repo_dir))
            out = []
            for entry in repo.get_walker(max_entries=limit):
                c = entry.commit
                out.append({
                    "sha": c.id.decode(),
                    "message": c.message.decode().strip(),
                    "timestamp": datetime.fromtimestamp(
                        c.author_time, tz=timezone.utc).isoformat(),
                })
            return out
        except Exception:
            return []

    # -- durable flush -----------------------------------------------------

    async def _debounced_flush(self, ws: Workspace) -> None:
        await asyncio.sleep(self.FLUSH_IDLE_S)
        await self._flush(ws)

    async def _flush(self, ws: Workspace) -> str | None:
        async with ws.lock:
            if not ws.dirty:
                return None
            # write files to disk
            for path, fst in ws.files.items():
                fp = ws.repo_dir / path
                fp.parent.mkdir(parents=True, exist_ok=True)
                if not fp.exists() or fp.read_text() != fst.text:
                    fp.write_text(fst.text)
            ws.dirty = False
        # commit outside the lock (dulwich is sync/blocking but short)
        ops = self._ops_since_flush.pop(ws.workspace_id, 0)
        actors = self._actors_since_flush.pop(ws.workspace_id, set())
        if self._commit_fn is not None:
            try:
                return self._commit_fn(ws, ops, actors)
            except Exception as e:
                log.warning("commit hook failed: %s", e)
                return None
        try:
            from dulwich import porcelain
            from dulwich.repo import Repo
            repo = Repo(str(ws.repo_dir))
            # stage all workspace files
            targets = [str(ws.repo_dir / p) for p in ws.files]
            if targets:
                porcelain.add(repo, targets)
            actor_str = ",".join(sorted(actors)[:3]) or "server"
            msg = (f"live checkpoint seq {ws.seq} ({ops} ops"
                   f" by {actor_str})").encode()
            sha = porcelain.commit(
                repo, message=msg,
                author=b"clique-server <server@clique>",
                committer=b"clique-server <server@clique>")
            return sha.decode() if isinstance(sha, bytes) else str(sha)
        except Exception as e:
            log.warning("workspace commit failed %s: %s", ws.workspace_id, e)
            return None

    async def force_flush(self, workspace_id: str) -> str | None:
        """Force immediate checkpoint (e.g. on agent turn end)."""
        ws = self.get(workspace_id)
        if ws._flush_task and not ws._flush_task.done():
            ws._flush_task.cancel()
        return await self._flush(ws)
