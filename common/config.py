"""Configuration + node identity (MVP implementation).

Config file: ~/.clique/config.toml (override with CLIQUE_CONFIG or an
explicit path). Keypair: <data_dir>/node.key (Ed25519 seed, hex).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from nacl.signing import SigningKey

from common.errors import ConfigError

DEFAULT_DIR = Path.home() / ".clique"


@dataclass
class NodeConfig:
    display_name: str = ""
    data_dir: Path = DEFAULT_DIR
    model_family: str = "echo"
    model_parameter_b: float = 7.0
    model_runtime: str = "echo"  # "echo" | "openai-compat"
    openai_base_url: str = "http://127.0.0.1:11434/v1"  # ollama default
    openai_model_name: str = ""  # e.g. "qwen2.5-coder:7b"
    context_window: int = 8192
    allow_battery_inference: bool = True
    heartbeat_interval_s: float = 2.0
    agent_port: int = 0  # 0 = agent does not listen; server pushes over WS


@dataclass
class ServerConfig:
    api_host: str = "0.0.0.0"
    api_port: int = 7777
    db_path: Path = DEFAULT_DIR / "server.db"
    default_model: str = "qwen2.5-coder-7b"
    heartbeat_offline_after: int = 3
    queue_cap: int = 1000
    lease_seconds: float = 120.0
    max_attempts: int = 3
    permission_policy: str = "first-client-op"
    clique_name: str = "clique"


@dataclass
class Config:
    node: NodeConfig = field(default_factory=NodeConfig)
    server: ServerConfig = field(default_factory=ServerConfig)


def _apply(section: dict, obj) -> None:
    for k, v in section.items():
        if not hasattr(obj, k):
            raise ConfigError(f"unknown config key: {k}")
        cur = getattr(obj, k)
        if isinstance(cur, Path):
            v = Path(v).expanduser()
        setattr(obj, k, v)


def load(path: Path | None = None) -> Config:
    cfg = Config()
    p = path or (Path(os.environ["CLIQUE_CONFIG"]) if "CLIQUE_CONFIG" in os.environ
                 else DEFAULT_DIR / "config.toml")
    if p.exists():
        try:
            data = tomllib.loads(p.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"invalid TOML in {p}: {e}") from e
        _apply(data.get("node", {}), cfg.node)
        _apply(data.get("server", {}), cfg.server)
    if not cfg.node.display_name:
        import socket
        cfg.node.display_name = socket.gethostname().split(".")[0]
    return cfg


def write_default(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# clique node configuration\n"
        "[node]\n"
        '# display_name = "my-laptop"\n'
        '# model_runtime = "openai-compat"   # or "echo"\n'
        '# openai_base_url = "http://127.0.0.1:11434/v1"\n'
        '# openai_model_name = "qwen2.5-coder:7b"\n'
        "# model_parameter_b = 7.0\n"
        "\n[server]\n"
        "# api_port = 7777\n"
    )


def generate_or_load_keypair(data_dir: Path) -> tuple[str, str]:
    """Return (public_key_hex, signing_seed_hex), creating on first run."""
    data_dir.mkdir(parents=True, exist_ok=True)
    key_file = data_dir / "node.key"
    if key_file.exists():
        seed_hex = key_file.read_text().strip()
    else:
        seed_hex = SigningKey.generate()._seed.hex()
        key_file.write_text(seed_hex)
        key_file.chmod(0o600)
    sk = SigningKey(bytes.fromhex(seed_hex))
    return sk.verify_key.encode().hex(), seed_hex
