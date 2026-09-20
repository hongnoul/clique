"""Tests for the headless MCP harness installer (client/mcp_install.py)."""

from __future__ import annotations

import json

import pytest

from client.mcp_install import find_binary, install, status, targets

BIN = "/opt/clique/.venv/bin/clique-mcp"


@pytest.fixture
def home(tmp_path):
    return tmp_path


def test_skips_absent_harnesses(home):
    results = install(home=home, binary=BIN)
    assert all(r.action == "skipped (not present)" for r in results)
    assert not list(home.iterdir())  # nothing scaffolded


def test_installs_into_present_json_harnesses(home):
    (home / ".cursor").mkdir()
    (home / ".claude.json").write_text("{}")
    results = {r.harness: r for r in install(home=home, binary=BIN,
                                             url="http://10.0.0.5:7777")}
    assert results["cursor"].action == "installed"
    assert results["claude-code"].action == "installed"
    assert results["codex"].action == "skipped (not present)"

    cfg = json.loads((home / ".cursor" / "mcp.json").read_text())
    entry = cfg["mcpServers"]["clique"]
    assert entry["command"] == BIN
    assert entry["env"] == {"CLIQUE_URL": "http://10.0.0.5:7777"}

    claude = json.loads((home / ".claude.json").read_text())
    assert claude["mcpServers"]["clique"]["command"] == BIN


def test_json_preserves_existing_config(home):
    (home / ".claude.json").write_text(json.dumps({
        "projects": {"/x": {"history": [1, 2]}},
        "mcpServers": {"other": {"command": "foo"}}}))
    install(home=home, binary=BIN)
    data = json.loads((home / ".claude.json").read_text())
    assert data["projects"] == {"/x": {"history": [1, 2]}}
    assert data["mcpServers"]["other"] == {"command": "foo"}
    assert data["mcpServers"]["clique"]["command"] == BIN


def test_toml_install_and_update(home):
    (home / ".codex").mkdir()
    (home / ".codex" / "config.toml").write_text(
        'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "foo"\n')
    r1 = {r.harness: r for r in install(home=home, binary=BIN)}
    assert r1["codex"].action == "installed"
    text = (home / ".codex" / "config.toml").read_text()
    assert 'model = "gpt-5"' in text
    assert "[mcp_servers.other]" in text
    assert f'command = "{BIN}"' in text

    # idempotent: second run updates in place, no duplicates
    r2 = {r.harness: r for r in install(home=home, binary=BIN,
                                        url="http://n:1")}
    assert r2["codex"].action == "updated"
    text = (home / ".codex" / "config.toml").read_text()
    assert text.count("[mcp_servers.clique]") == 1
    assert 'CLIQUE_URL = "http://n:1"' in text
    # valid TOML round-trip
    import tomllib
    parsed = tomllib.loads(text)
    assert parsed["mcp_servers"]["clique"]["command"] == BIN
    assert parsed["mcp_servers"]["other"]["command"] == "foo"


def test_json_update_is_idempotent(home):
    (home / ".cursor").mkdir()
    install(home=home, binary=BIN)
    r2 = {r.harness: r for r in install(home=home, binary="/new/bin")}
    assert r2["cursor"].action == "updated"
    cfg = json.loads((home / ".cursor" / "mcp.json").read_text())
    assert cfg["mcpServers"]["clique"]["command"] == "/new/bin"


def test_status_reports(home):
    (home / ".cursor").mkdir()
    assert all(r.action == "absent" for r in status(home=home))
    install(home=home, binary=BIN)
    st = {r.harness: r.action for r in status(home=home)}
    assert st["cursor"] == "registered"
    assert st["codex"] == "absent"


def test_module_fallback_split(home):
    (home / ".cursor").mkdir()
    (home / ".codex").mkdir()
    binary = "/usr/bin/python3 -m client.mcp_server"
    install(home=home, binary=binary)
    cfg = json.loads((home / ".cursor" / "mcp.json").read_text())
    entry = cfg["mcpServers"]["clique"]
    assert entry["command"] == "/usr/bin/python3"
    assert entry["args"] == ["-m", "client.mcp_server"]
    import tomllib
    parsed = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert parsed["mcp_servers"]["clique"]["args"] == \
        ["-m", "client.mcp_server"]


def test_find_binary_returns_absolute_or_module():
    b = find_binary()
    assert b.startswith("/") or " -m " in b


def test_all_targets_under_home(home):
    for _, path, _ in targets(home):
        assert str(path).startswith(str(home))


# -- grow ---------------------------------------------------------------

def _mk_server(base: Path, name: str, body: str) -> Path:
    d = base / "dogfood-mcp" / name
    d.mkdir(parents=True)
    (d / "server.py").write_text(body)
    return d


_GOOD = '"""plume draft helper."""\nfrom mcp.server import Server\nprint("mcp up")\n'


def test_grow_registers_valid_server(home):
    from client.mcp_install import discover_grown, grow
    (home / ".cursor").mkdir()
    (home / ".jcode").mkdir()
    _mk_server(home, "plume", _GOOD)
    servers, _ = grow(home=home, binary="/usr/bin/clique-mcp", check=True)
    assert [s.name for s in servers] == ["plume"]
    assert servers[0].valid, servers[0].problem
    cursor = json.loads((home / ".cursor" / "mcp.json").read_text())
    assert "plume" in cursor["mcpServers"]
    jcode_cfg = (home / ".jcode" / "config.toml").read_text()
    assert "[mcp_servers.plume]" in jcode_cfg


def test_grow_skips_broken_server(home):
    from client.mcp_install import grow
    (home / ".cursor").mkdir()
    _mk_server(home, "bad", "def broken(:\n")
    servers, _ = grow(home=home, binary="/usr/bin/clique-mcp", check=True)
    assert len(servers) == 1 and not servers[0].valid
    cursor = json.loads((home / ".cursor" / "mcp.json").read_text())
    assert "bad" not in cursor.get("mcpServers", {})
    assert "clique" in cursor["mcpServers"]  # clique still registers


def test_grow_empty_dir_is_quiet(home):
    from client.mcp_install import discover_grown, grow
    assert discover_grown(home) == []
    servers, _ = grow(home=home, binary="/usr/bin/clique-mcp", check=False)
    assert servers == []


def test_grow_mcp_json_override(home):
    from client.mcp_install import discover_grown
    d = _mk_server(home, "node-tool", "placeholder mcp server\n")
    (d / "mcp.json").write_text(json.dumps({"command": "node",
                                            "args": ["server.js"]}))
    servers = discover_grown(home)
    assert servers[0].command == "node"
    assert servers[0].args == ["server.js"]
