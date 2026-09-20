"""Deterministic code-edit harness — P0 interfaces.

Server-side only. Nodes stay raw inference: they return free text, and
this module extracts one fenced unified diff, validates guards, and
builds the code prompt. Applying + test-verifying lives in
``scheduler/workspaces.py``.

Patch wire format (strict, single fence):

```diff
--- a/foo.py
+++ b/foo.py
@@ ...
-old
+new
```
"""

from __future__ import annotations

import re

from common.errors import ProtocolError
from common.types import CodeTaskSpec

_FENCE_RE = re.compile(r"```diff\n(.*?)```", re.DOTALL)
_TEST_ALLOWLIST = ("pytest", "cargo", "go", "npm", "pnpm", "make")


def extract_diff(output: str) -> str:
    """Return the single fenced diff body. Raise ProtocolError otherwise."""
    if not output:
        raise ProtocolError("diff_missing: empty model output")
    fences = _FENCE_RE.findall(output)
    if not fences:
        raise ProtocolError("diff_missing: no ```diff fence found")
    if len(fences) > 1:
        raise ProtocolError("diff_invalid: multiple ```diff fences")
    diff = fences[0].strip() + "\n"
    if not diff.startswith("--- ") and "diff --git" not in diff:
        raise ProtocolError("diff_invalid: not a unified diff")
    return diff


def _touched_paths(diff: str) -> list[str]:
    paths = []
    for line in diff.splitlines():
        if line.startswith(("--- a/", "+++ b/")):
            paths.append(line[6:])
        elif line.startswith(("--- ", "+++ ")) and "dev/null" not in line:
            paths.append(line[4:].lstrip("a/b/"))
    return paths


def validate_diff(diff: str, spec: CodeTaskSpec) -> None:
    """Enforce size + path guards. Raise ProtocolError on violation."""
    size_kb = len(diff.encode()) / 1024
    if size_kb > spec.patch_budget_kb:
        raise ProtocolError(
            f"patch_too_large: {size_kb:.1f}kb over {spec.patch_budget_kb}kb")
    for path in _touched_paths(diff):
        if not path or path == "/dev/null":
            continue
        if path.startswith("/") or ".." in path.split("/"):
            raise ProtocolError(f"path_forbidden: {path}")
        if "\x00" in path:
            raise ProtocolError(f"path_forbidden: binary path {path!r}")
        if spec.allowed_paths and not any(
                path == a or path.startswith(a.rstrip("/") + "/")
                for a in spec.allowed_paths):
            raise ProtocolError(f"path_forbidden: {path} not in allowed_paths")
    cmd = spec.test_cmd or []
    if not cmd or cmd[0] not in _TEST_ALLOWLIST:
        raise ProtocolError(f"test_cmd_forbidden: {cmd[:1]}")


def build_code_prompt(task_prompt: str, spec: CodeTaskSpec,
                       context_budget_chars: int = 24000) -> str:
    """Pack task + file snapshot into a raw-inference prompt.

    Output reserve is left to the caller via max_output_tokens.
    Files are packed changed-first by insertion order, truncated to fit.
    """
    header = (
        "You are a code-edit bot. Emit exactly one unified diff inside a "
        "single ```diff fence. No other file writes, no shell. "
        "Paths relative to repo root (a/ b/ prefixes). Keep the diff "
        "minimal and complete.\n\n"
        f"Task: {task_prompt}\n"
        f"Base: {spec.base_sha or '(fresh snapshot)'}\n"
        f"Tests: {' '.join(spec.test_cmd)}\n\nFiles:\n"
    )
    body_parts: list[str] = []
    used = len(header)
    for path, content in spec.files.items():
        chunk = f"\n--- file: {path} ---\n{content}\n"
        if used + len(chunk) > context_budget_chars:
            remaining = context_budget_chars - used
            if remaining > 200:
                body_parts.append(chunk[:remaining] + "\n[truncated]\n")
            break
        body_parts.append(chunk)
        used += len(chunk)
    return header + "".join(body_parts)
