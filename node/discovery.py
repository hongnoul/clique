"""Clique discovery over the local network.

Uses mDNS/DNS-SD via python-zeroconf. Service type: ``_clique._tcp.local.``
The server node announces; joining nodes browse. All nodes in a clique
must be on the same network (vision dump), which mDNS assumes anyway.

Server election is intentionally dumb for now: first node to start with
server capability announces itself; everyone else joins it. No Raft.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ServerAnnouncement:
    """What the server node publishes in its mDNS TXT record."""

    clique_name: str
    api_address: str  # host:port of scheduler/api
    server_public_key_fingerprint: str
    protocol_version: int


async def find_server(timeout_s: float = 5.0) -> ServerAnnouncement | None:
    """Browse for an existing server node.

    Returns the announcement of the first (and expected only) server
    found, or None after timeout. Multiple simultaneous announcements
    are a split-brain misconfiguration: log loudly and pick the one with
    the lexicographically smallest fingerprint so all joiners agree.
    """
    ...


async def announce_server(announcement: ServerAnnouncement) -> "AnnouncementHandle":
    """Publish this node as the clique's server. Returns a handle whose
    close() withdraws the record (used on shutdown/demotion)."""
    ...


class AnnouncementHandle:
    async def close(self) -> None: ...


async def watch_server(
    current: ServerAnnouncement,
    on_lost: "callable[[], None]",
) -> None:
    """Monitor the server's mDNS presence; invoke on_lost if it
    disappears so the agent can retry discovery (future: trigger
    re-election / promote the op node)."""
    ...
