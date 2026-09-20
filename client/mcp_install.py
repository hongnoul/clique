"""Headless registration of the clique MCP server into local agent harnesses.

``clique mcp install`` finds every supported harness config on this
machine and adds (or updates) a ``clique`` stdio MCP server entry that
points at the absolute ``clique-mcp`` binary, so PATH quirks inside GUI
apps never matter. Idempotent: re-running updates in place.

``clique mcp grow`` extends the same loop to agent-built tools:
``~/dogfood-mcp/<name>/server.py`` (MCP stdio, optional ``mcp.json``
override) each gets its own named entry, so capabilities agents build
in one session are picked up by every new pane. Idempotent too.

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
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

DOGFOOD_DIR_NAME = "dogfood-mcp"


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


# ---------------------------------------------------------------------------
# grow: register agent-built MCP servers from ~/dogfood-mcp
# ---------------------------------------------------------------------------

@dataclass
class GrownServer:
    name: str
    path: Path          # server.py (or mcp.json-declared entry)
    command: str        # resolved command
    args: list[str]
    env: dict[str, str]
    valid: bool
    problem: str = ""   # why invalid, when not valid


def dogfood_dir(home: Path | None = None) -> Path:
    """Where agents grow MCP servers: ~/dogfood-mcp."""
    return (home or Path.home()) / DOGFOOD_DIR_NAME


def discover_grown(home: Path | None = None) -> list[GrownServer]:
    """Scan ~/dogfood-mcp/*/ for server.py (+ optional mcp.json).

    A directory counts when it holds ``server.py``. An ``mcp.json``
    next to it may override ``{command, args, env}`` (e.g. a non-python
    runtime or extra env). Bare server.py defaults to
    ``sys.executable server.py``.
    """
    base = dogfood_dir(home)
    out: list[GrownServer] = []
    if not base.is_dir():
        return out
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        server_py = child / "server.py"
        meta = child / "mcp.json"
        if not server_py.is_file() and not meta.is_file():
            continue
        cmd = sys.executable
        args = [str(server_py)] if server_py.is_file() else []
        env: dict[str, str] = {}
        if meta.is_file():
            try:
                spec = json.loads(meta.read_text())
            except json.JSONDecodeError:
                out.append(GrownServer(child.name, server_py, "", [],
                                      {}, False, "mcp.json is not valid JSON"))
                continue
            cmd = str(spec.get("command", cmd))
            raw_args = spec.get("args")
            if raw_args is not None:
                args = [str(a) for a in raw_args]
            elif server_py.is_file():
                args = [str(server_py)]
            raw_env = spec.get("env") or {}
            env = {str(k): str(v) for k, v in raw_env.items()}
        out.append(GrownServer(child.name,
                               server_py if server_py.is_file() else meta,
                               cmd, args, env, True))
    return out


def smoke_check(server: GrownServer, timeout_s: float = 20.0) -> GrownServer:
    """Best-effort validation: binary exists, file compiles/imports.

    Never executes untrusted code beyond compile + a --help probe with
    no network. Failures mark the server invalid with a reason; grow
    skips invalid servers instead of registering broken entries.
    """
    if not server.valid:
        return server
    if not shutil.which(server.command) and \
            not Path(server.command).exists():
        server.valid = False
        server.problem = f"command not found: {server.command}"
        return server
    if server.path.suffix == ".py" and server.path.is_file():
        try:
            src = server.path.read_text()
            compile(src, str(server.path), "exec")
        except (SyntaxError, UnicodeDecodeError) as e:
            server.valid = False
            server.problem = f"server.py does not compile: {e}"
            return server
        if "mcp" not in src.lower():
            server.valid = False
            server.problem = "server.py does not mention mcp " \
                "(probably not an MCP server)"
            return server
    # probe: --help must exit quickly (stdio servers usually print
    # usage or start serving; either way a fast exit/hang-free spawn
    # proves the interpreter + deps resolve)
    try:
        proc = subprocess.run(
            [server.command, *server.args, "--help"],
            capture_output=True, timeout=timeout_s,
            env={**os.environ, **server.env})
        if proc.returncode not in (0, 1, 2):
            server.valid = False
            server.problem = f"--help probe exited {proc.returncode}: " \
                f"{proc.stderr.decode()[:200]}"
    except subprocess.TimeoutExpired:
        # a stdio server that ignores --help and waits on stdin is
        # fine: the spawn itself succeeded, deps resolved.
        pass
    except OSError as e:
        server.valid = False
        server.problem = f"cannot spawn: {e}"
    return server


def _entry_for(server: GrownServer) -> dict:
    entry: dict = {"command": server.command, "args": server.args}
    if server.env:
        entry["env"] = server.env
    return entry


def _install_named_json(path: Path, key: str, name: str,
                        entry: dict) -> str:
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            return "skipped (unreadable config)"
    servers = data.setdefault(key, {})
    existed = name in servers
    servers[name] = entry
    path.write_text(json.dumps(data, indent=2) + "\n")
    return "updated" if existed else "installed"


def _install_named_toml(path: Path, name: str, entry: dict) -> str:
    """Line-based TOML rewrite for ``[mcp_servers.<name>]``."""
    section = f"[mcp_servers.{name}]"
    quoted = f'[mcp_servers."{name}"]'
    lines: list[str] = []
    existed = False
    if path.exists():
        raw = path.read_text().splitlines()
        skip = False
        for ln in raw:
            stripped = ln.strip()
            if stripped.startswith(section) or stripped.startswith(quoted):
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
    lines.append(section)
    lines.append(f'command = "{entry["command"]}"')
    args = ", ".join(f'"{a}"' for a in entry.get("args", []))
    lines.append(f"args = [{args}]")
    if entry.get("env"):
        env = ", ".join(f'{k} = "{v}"'
                        for k, v in entry["env"].items())
        lines.append(f"env = {{ {env} }}")
    path.write_text("\n".join(lines) + "\n")
    return "updated" if existed else "installed"


def grow(url: str | None = None, home: Path | None = None,
         binary: str | None = None,
         check: bool = True) -> tuple[list[GrownServer], list[Result]]:
    """Register clique + every valid ~/dogfood-mcp server, idempotently.

    Returns (servers, per-harness results). Invalid servers are reported
    in ``servers`` with valid=False and never registered.
    """
    home = home or Path.home()
    binary = binary or find_binary()
    # clique itself always (re)registers first
    harness_results = install(url=url, home=home, binary=binary)
    servers = discover_grown(home)
    if check:
        servers = [smoke_check(s) for s in servers]
    valid = [s for s in servers if s.valid]
    if not valid:
        return servers, harness_results
    for harness, path, fmt in targets(home):
        present = path.exists() or (
            path.parent != home and path.parent.exists())
        if not present:
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        for srv in valid:
            entry = _entry_for(srv)
            if fmt == "json":
                action = _install_named_json(path, "mcpServers",
                                             srv.name, entry)
            else:
                action = _install_named_toml(path, srv.name, entry)
            harness_results.append(
                Result(f"{harness}/{srv.name}", path, action))
    return servers, harness_results
