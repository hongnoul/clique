"""Ephemeral per-task workspaces — P0 interfaces.

Each CODE_EDIT task gets ``<base_dir>/<task_id>`` seeded from the
client-supplied file snapshot. The harness applies the extracted diff
here, runs the allowlisted test command with no network, and on success
the caller commits to the canonical code-repo (a ``VcsService``).

P0 implements create/apply/run_tests/cleanup with stdlib only
(``patch`` CLI when present, otherwise a minimal unidiff applier for
single-hunk adds/modifies). No shell string: test_cmd is list-form
subprocess with timeout.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path

from common.errors import CliqueError
from common.types import CodeTaskSpec

log = logging.getLogger("clique.workspaces")

_GOOD_HUNK_RE = re.compile(r"^@@ -\d")


def _normalize_hunk_headers(ws: Path, diff: str) -> str:
    """Fill in line ranges for bare ``@@`` hunk headers.

    Models routinely emit ``@@`` with no ``-start,len +start,len``
    ranges; GNU patch(1) rejects that as garbage even though the hunk
    body is fine. Locate each bare hunk's old-side lines in the target
    file and rewrite the header. Well-formed headers pass through
    untouched; unlocatable hunks are left for patch(1) to report.
    """
    lines = diff.splitlines()
    out: list[str] = []
    i = 0
    file_lines: list[str] = []
    search_from = 0
    delta = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("+++ "):
            path = line[4:].strip()
            rel = path.split("/", 1)[1] if "/" in path else path  # -p1
            target = ws / rel
            file_lines = (target.read_text().splitlines()
                          if target.is_file() else [])
            search_from, delta = 0, 0
            out.append(line)
            i += 1
            continue
        if line.startswith("@@") and not _GOOD_HUNK_RE.match(line):
            j = i + 1
            body: list[str] = []
            while j < len(lines) and not lines[j].startswith(
                    ("@@", "--- ", "+++ ", "diff ")):
                # blank line inside a hunk is a context line whose
                # trailing space got stripped
                body.append(lines[j] if lines[j] else " ")
                j += 1
            while body and body[-1] == " " and j >= len(lines):
                body.pop()  # trailing blank(s) after the last hunk
            old_side = [b[1:] for b in body if b[:1] in (" ", "-")]
            n = len(old_side)
            pos = -1
            if n == 0:
                old_start, old_len = 0, 0
                new_start = 1 + delta
            else:
                for k in range(search_from, len(file_lines) - n + 1):
                    if file_lines[k:k + n] == old_side:
                        pos = k
                        break
                if pos < 0:
                    out.append(line)
                    i += 1
                    continue
                old_start, old_len = pos + 1, n
                new_start = old_start + delta
            new_len = sum(1 for b in body if b[:1] in (" ", "+"))
            delta += new_len - old_len
            if pos >= 0:
                search_from = pos + n
            out.append(f"@@ -{old_start},{old_len} +{new_start},{new_len} @@")
            out.extend(body)
            i = j
            continue
        out.append(line)
        i += 1
    return "\n".join(out) + "\n"


class WorkspaceError(CliqueError):
    """Checkout, patch, or test failure with a machine-readable prefix."""


class WorkspaceManager:
    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, task_id: str) -> Path:
        if not task_id or "/" in task_id or ".." in task_id:
            raise WorkspaceError(f"bad_task_id: {task_id!r}")
        return self.base_dir / task_id

    def create(self, task_id: str, spec: CodeTaskSpec) -> Path:
        """Seed the workspace from the file snapshot. Text files only."""
        ws = self.path_for(task_id)
        if ws.exists():
            shutil.rmtree(ws)
        ws.mkdir(parents=True)
        for rel, content in spec.files.items():
            if rel.startswith("/") or ".." in Path(rel).parts:
                raise WorkspaceError(f"path_forbidden: {rel}")
            if "\x00" in content:
                raise WorkspaceError(f"binary_rejected: {rel}")
            target = ws / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return ws

    def apply_patch(self, task_id: str, diff: str) -> None:
        ws = self.path_for(task_id)
        if not ws.exists():
            raise WorkspaceError("no_workspace")
        diff = _normalize_hunk_headers(ws, diff)
        patch_file = ws / ".harness.patch"
        patch_file.write_text(diff)
        # Prefer system patch(1) dry-run then apply; fallback to minimal applier.
        if shutil.which("patch"):
            dry = subprocess.run(
                ["patch", "-p1", "--dry-run", "-i", str(patch_file)],
                cwd=ws, capture_output=True, text=True, timeout=15)
            if dry.returncode != 0:
                raise WorkspaceError(f"patch_conflict: {dry.stderr.strip()[:300]}")
            res = subprocess.run(
                ["patch", "-p1", "-s", "-i", str(patch_file)],
                cwd=ws, capture_output=True, text=True, timeout=30)
            if res.returncode != 0:
                raise WorkspaceError(f"patch_conflict: {res.stderr.strip()[:300]}")
            return
        self._apply_simple(ws, diff)

    def _apply_simple(self, ws: Path, diff: str) -> None:
        """Minimal applier: whole-file add/modify via ---/+++ + single hunk.

        Full unidiff generality comes later (or `unidiff` dep). Good
        enough for P0 fixtures and small model outputs.
        """
        cur: str | None = None
        old_lines: list[str] = []
        new_lines: list[str] = []
        in_hunk = False

        def flush() -> None:
            if cur is None:
                return
            target = ws / cur.removeprefix("b/").removeprefix("a/")
            if not old_lines and new_lines:  # new file
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("".join(new_lines))
            else:
                text = target.read_text() if target.exists() else ""
                lines = text.splitlines(keepends=True)
                # naive: verify removals present, then append additions
                for rm in old_lines:
                    if rm not in lines:
                        raise WorkspaceError(f"patch_conflict: {cur} context miss")
                    lines.remove(rm)
                lines.extend(new_lines)
                target.write_text("".join(lines))

        for line in diff.splitlines(keepends=True):
            if line.startswith("+++ "):
                flush()
                cur = line[4:].strip()
                old_lines, new_lines, in_hunk = [], [], False
            elif line.startswith("@@"):
                in_hunk = True
            elif in_hunk and cur is not None:
                if line.startswith("-"):
                    old_lines.append(line[1:])
                elif line.startswith("+"):
                    new_lines.append(line[1:])
        flush()
        if cur is None:
            raise WorkspaceError("patch_conflict: no file headers")

    def run_tests(self, task_id: str, spec: CodeTaskSpec) -> dict:
        """Run the allowlisted test_cmd in the workspace. No shell, timeout."""
        ws = self.path_for(task_id)
        cmd = list(spec.test_cmd or [])
        # Resolve bare `pytest` to the running interpreter so sandboxes
        # without pytest on PATH still verify (same test runner version).
        if cmd and cmd[0] == "pytest":
            import sys as _sys
            cmd = [_sys.executable, "-m", "pytest", *cmd[1:]]
        try:
            import os as _os
            env = dict(_os.environ)
            env["NO_NETWORK"] = "1"
            proc = subprocess.run(
                cmd, cwd=ws, capture_output=True, text=True,
                timeout=spec.timeout_s, env=env)
        except subprocess.TimeoutExpired:
            raise WorkspaceError("timeout: test_cmd exceeded timeout_s")
        report = {"cmd": cmd, "returncode": proc.returncode,
                  "stdout_tail": proc.stdout[-2000:],
                  "stderr_tail": proc.stderr[-2000:]}
        if proc.returncode != 0:
            err = WorkspaceError(f"tests_failed: rc={proc.returncode}")
            err.report = report  # surface via /v1/code/tasks/{id}/tests
            raise err
        return report

    def cleanup(self, task_id: str) -> None:
        shutil.rmtree(self.path_for(task_id), ignore_errors=True)
