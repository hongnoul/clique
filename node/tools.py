"""Node-local tool sandbox — deterministic file ops for the tool loop.

The model never gets a shell. It emits newline-delimited JSON tool calls
in its text output; this module parses and applies them inside a sandbox
dir seeded from the assignment. Allowed ops: read, edit (exact-match
replace), write (new file only), bash (allowlisted test runners only).

Server still reverifies the final diff before commit. This loop only
improves proposal quality on capable models; echo/small models take the
single-shot path in the executor.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from common.errors import CliqueError

MAX_STEPS = 5
_BASH_ALLOWLIST = ("pytest", "python", "cargo", "go", "npm", "make")


class ToolError(CliqueError):
    """Tool call rejected or failed."""


def parse_tool_calls(text: str) -> list[dict]:
    """Extract JSON tool-call lines. Ignores prose and diff fences.

    A tool-call line is a JSON object on its own line with an "op" key.
    Raises ToolError on malformed JSON lines that look like calls.
    """
    calls = []
    in_fence = False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
            except json.JSONDecodeError as e:
                raise ToolError(f"bad_tool_json: {e}") from e
            if isinstance(obj, dict) and "op" in obj:
                calls.append(obj)
    return calls


class Sandbox:
    """Seeded working dir with guarded file ops + test runner."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def seed(self, files: dict[str, str]) -> None:
        import shutil
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True)
        for rel, content in files.items():
            self._guard_rel(rel)
            if "\x00" in content:
                raise ToolError(f"binary_rejected: {rel}")
            target = self.root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def _guard_rel(self, rel: str) -> Path:
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise ToolError(f"path_forbidden: {rel}")
        target = (self.root / rel).resolve()
        if target != self.root.resolve() and \
                self.root.resolve() not in target.parents:
            raise ToolError(f"path_escape: {rel}")
        return target

    def read(self, path: str) -> str:
        target = self._guard_rel(path)
        if not target.is_file():
            raise ToolError(f"no_such_file: {path}")
        text = target.read_text()
        return text[:20000]

    def edit(self, path: str, old: str, new: str) -> str:
        """Exact-match single replacement. Old must occur exactly once."""
        target = self._guard_rel(path)
        if not target.is_file():
            raise ToolError(f"no_such_file: {path}")
        text = target.read_text()
        count = text.count(old)
        if count != 1:
            raise ToolError(
                f"edit_ambiguous: {path} matches {count}x (need exactly 1)")
        target.write_text(text.replace(old, new))
        return f"edited {path}"

    def write(self, path: str, content: str) -> str:
        """Create a new file only. Refuses to overwrite."""
        target = self._guard_rel(path)
        if target.exists():
            raise ToolError(f"write_refused_overwrite: {path}")
        if "\x00" in content:
            raise ToolError(f"binary_rejected: {path}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return f"wrote {path}"

    def bash(self, cmd: list[str], timeout_s: int = 60) -> str:
        if not cmd or cmd[0] not in _BASH_ALLOWLIST:
            raise ToolError(f"bash_forbidden: {cmd[:1]}")
        resolved = list(cmd)
        if resolved[0] == "pytest":
            import sys as _sys
            resolved = [_sys.executable, "-m", "pytest", *resolved[1:]]
        import os as _os
        env = dict(_os.environ)
        env["NO_NETWORK"] = "1"
        try:
            proc = subprocess.run(
                resolved, cwd=self.root, capture_output=True, text=True,
                timeout=timeout_s, env=env)
        except subprocess.TimeoutExpired:
            raise ToolError("timeout: bash exceeded timeout_s")
        tail = (proc.stdout + proc.stderr)[-3000:]
        return f"rc={proc.returncode}\n{tail}"

    def apply_call(self, call: dict) -> str:
        op = call.get("op")
        if op == "read":
            return self.read(call["path"])
        if op == "edit":
            return self.edit(call["path"], call["old"], call["new"])
        if op == "write":
            return self.write(call["path"], call.get("content", ""))
        if op == "bash":
            cmd = call["cmd"]
            if isinstance(cmd, str):
                import shlex as _sh
                cmd = _sh.split(cmd)
            return self.bash(cmd, call.get("timeout_s", 60))
        if op == "done":
            return "done"
        raise ToolError(f"unknown_op: {op}")

    def snapshot(self) -> dict[str, str]:
        """Current sandbox files (text only, skips caches)."""
        out = {}
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(self.root))
            if rel.split("/")[0] in ("__pycache__", ".pytest_cache"):
                continue
            if p.suffix in (".pyc", ".pyo"):
                continue
            try:
                out[rel] = p.read_text()
            except UnicodeDecodeError:
                continue
        return out
