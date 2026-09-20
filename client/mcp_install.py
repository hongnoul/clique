"""Headless registration of the clique MCP server into local agent harnesses.

``clique mcp install`` finds every supported harness config on this
machine and adds (or updates) a ``clique`` stdio MCP server entry that
points at the absolute ``clique-mcp`` binary, so PATH quirks inside GUI
apps never matter. Idempotent: re-running updates in place.

Supported harnesses:
  - Claude Code            (~/.claude.json, ``mcpServers``)
  - Codex CLI              (~/.codex/config.toml, ``[mcp_servers.clique]``)
  - Cursor                 (~/.cursor/mcp.json)
  - Windsurf               (~/.codeium/windsurf/mcp_config.json)
  - Claude Desktop (macOS) (~/Library/Application Support/Claude/
                            claude_desktop_config.json)
  - Jcode                  (~/.jcode/config.toml, ``[mcp_servers.clique]``)

Only configs whose parent directory already exists are touched, so we
never scaffold a harness the user does not have. ``--url`` pins
CLIQUE_URL; omitting it leaves discovery to mDNS at call time.
"""

from __future__ import annotations

import json
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path


def find_binary() -> str:
    """Absolute path to clique-mcp, preferring this interpreter's env."""
    sibling = Path(sys.executable).parent / "clique-mcp"
    if sibling.exists():
        return str(sibling)
    found = shutil.which("clique-mcp")
    if found:
        return found
    # last resort: run the module with this interpreter (repo checkouts)
    return f"{sys.executable} -m client.mcp_server"


@dataclass
class Result:
    harness: str
    path: Path
    action: str  # "installed" | "updated" | "skipped (not present)"


def _server_entry(binary: str, url: str | None) -> dict:
    entry: dict = {"command": binary, "args": []}
    if " -m " in binary:  # module fallback: split into command + args
        parts = binary.split()
        entry = {"command": parts[0], "args": parts[1:]}
    if url:
        entry["env"] = {"CLIQUE_URL": url}
    return entry


def _install_json(path: Path, key: str, binary: str,
                  url: str | None) -> str:
    """Insert/update the clique entry in a JSON config's mcpServers map."""
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return "skipped (unreadable config)"
    servers = data.setdefault(key, {})
    existed = "clique" in servers
    servers["clique"] = _server_entry(binary, url)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return "updated" if existed else "installed"


def _install_toml(path: Path, binary: str, url: str | None) -> str:
    """Insert/update ``[mcp_servers.clique]`` in a TOML config.

    Line-based rewrite (stdlib has no TOML writer): drops any existing
    clique section, appends the fresh one. Preserves everything else.
    """
    lines: list[str] = []
    existed = False
    if path.exists():
        raw = path.read_text().splitlines()
        skip = False
        for ln in raw:
            stripped = ln.strip()
            if stripped.startswith("[mcp_servers.clique]") or \
                    stripped.startswith('[mcp_servers."clique"]'):
                skip = True
                existed = True
                continue
            if skip and stripped.startswith("["):
                skip = False
            if not skip:
                lines.append(ln)
    while lines and not lines[-1].strip():
        lines.pop()
    lines.append("")
    lines.append("[mcp_servers.clique]")
    if " -m " in binary:
        parts = binary.split()
        lines.append(f'command = "{parts[0]}"')
        args = ", ".join(f'"{a}"' for a in parts[1:])
        lines.append(f"args = [{args}]")
    else:
        lines.append(f'command = "{binary}"')
    if url:
        lines.append(f'env = {{ CLIQUE_URL = "{url}" }}')
    path.write_text("\n".join(lines) + "\n")
    return "updated" if existed else "installed"


def targets(home: Path) -> list[tuple[str, Path, str]]:
    """(harness, config path, format) for every supported harness."""
    return [
        ("claude-code", home / ".claude.json", "json"),
        ("codex", home / ".codex" / "config.toml", "toml"),
        ("cursor", home / ".cursor" / "mcp.json", "json"),
        ("windsurf",
         home / ".codeium" / "windsurf" / "mcp_config.json", "json"),
        ("claude-desktop",
         home / "Library" / "Application Support" / "Claude" /
         "claude_desktop_config.json", "json"),
        ("jcode", home / ".jcode" / "config.toml", "toml"),
    ]


def install(url: str | None = None, home: Path | None = None,
            binary: str | None = None) -> list[Result]:
    """Register clique-mcp with every harness present on this machine."""
    home = home or Path.home()
    binary = binary or find_binary()
    results: list[Result] = []
    for harness, path, fmt in targets(home):
        # presence heuristic: harness dir (or the config itself) exists
        present = path.exists() or (
            path.parent != home and path.parent.exists())
        if not present:
            results.append(Result(harness, path, "skipped (not present)"))
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "json":
            action = _install_json(path, "mcpServers", binary, url)
        else:
            action = _install_toml(path, binary, url)
        results.append(Result(harness, path, action))
    return results


def status(home: Path | None = None) -> list[Result]:
    """Report which harnesses currently have a clique entry."""
    home = home or Path.home()
    results: list[Result] = []
    for harness, path, fmt in targets(home):
        state = "absent"
        if path.exists():
            text = path.read_text()
            if fmt == "json":
                try:
                    if "clique" in json.loads(text).get("mcpServers", {}):
                        state = "registered"
                except json.JSONDecodeError:
                    state = "unreadable"
            elif "[mcp_servers.clique]" in text or \
                    '[mcp_servers."clique"]' in text:
                state = "registered"
        results.append(Result(harness, path, state))
    return results
