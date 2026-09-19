"""Configuration loading for node agents and the server node.

Config lives at ``~/.clique/config.toml`` (overridable by CLI flag or the
``CLIQUE_CONFIG`` env var). Parsed with stdlib ``tomllib``, validated with
pydantic in implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class NodeConfig:
    """Per-node settings (every device has one)."""

    display_name: str
    data_dir: Path  # keys, model cache, logs
    model: str | None  # cluster key / HF repo id; None = clique default
    max_memory_mb: int | None  # owner-set cap, None = auto
    allow_battery_inference: bool  # default False
    heartbeat_interval_s: float
    agent_port: int


@dataclass
class ServerConfig:
    """Extra settings when this node is the server node."""

    api_port: int
    db_path: Path  # sqlite file, git-versioned by scheduler/vcs.py
    default_model: str  # "qwen2.5-coder-7b-q4" per vision dump
    heartbeat_offline_after: int  # missed heartbeats before OFFLINE
    queue_cap: int
    permission_policy: str  # "first-client-op" | "open" | "democracy"


@dataclass
class Config:
    node: NodeConfig
    server: ServerConfig | None  # present only on the server node


def load(path: Path | None = None) -> Config:
    """Load and validate config.

    Resolution order: explicit path > $CLIQUE_CONFIG > ~/.clique/config.toml.
    Missing file yields defaults (default model, auto ports) so `clique join`
    works with zero config. Raises ConfigError on invalid values.
    """
    ...


def write_default(path: Path) -> None:
    """Write a commented default config for first run."""
    ...


def generate_or_load_keypair(data_dir: Path) -> tuple[bytes, bytes]:
    """Return (public, private) PyNaCl keys, creating them on first run.

    The node_id is derived from the public key, so keys define identity.
    """
    ...
