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
