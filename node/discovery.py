"""Clique discovery over the local network (MVP implementation).

mDNS/DNS-SD via python-zeroconf. Service type: _clique._tcp.local.
Server announces; joiners browse. Falls back cleanly when zeroconf is
unavailable (e.g. sandboxed tests use explicit --server URLs).
"""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass

SERVICE_TYPE = "_clique._tcp.local."


@dataclass
class ServerAnnouncement:
    clique_name: str
    api_address: str  # host:port
    server_public_key_fingerprint: str
    protocol_version: int


def _local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no packet sent for UDP connect
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


async def find_server(timeout_s: float = 5.0) -> ServerAnnouncement | None:
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

    found: list[ServerAnnouncement] = []
    done = asyncio.Event()
    loop = asyncio.get_running_loop()

    class Listener(ServiceListener):
        def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
            info = zc.get_service_info(type_, name, timeout=int(timeout_s * 1000))
            if not info:
                return
            props = {k.decode(): v.decode() for k, v in info.properties.items() if v}
            addrs = info.parsed_addresses()
            if not addrs:
                return
            found.append(ServerAnnouncement(
                clique_name=props.get("clique", "clique"),
                api_address=f"{addrs[0]}:{info.port}",
                server_public_key_fingerprint=props.get("fp", ""),
                protocol_version=int(props.get("v", "1")),
            ))
            loop.call_soon_threadsafe(done.set)

        def update_service(self, *a) -> None: ...
        def remove_service(self, *a) -> None: ...

    zc = Zeroconf()
    browser = ServiceBrowser(zc, SERVICE_TYPE, Listener())
    try:
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            pass
    finally:
        browser.cancel()
        zc.close()
    if not found:
        return None
    # split-brain rule: everyone agrees on the smallest fingerprint
    return sorted(found, key=lambda a: a.server_public_key_fingerprint)[0]


class AnnouncementHandle:
    def __init__(self, zc, info) -> None:
        self._zc = zc
        self._info = info

    async def close(self) -> None:
        try:
            self._zc.unregister_service(self._info)
            self._zc.close()
        except Exception:
            pass


async def announce_server(announcement: ServerAnnouncement) -> AnnouncementHandle:
    from zeroconf import ServiceInfo, Zeroconf

    host, port = announcement.api_address.rsplit(":", 1)
    if host in ("0.0.0.0", ""):
        host = _local_ip()
    info = ServiceInfo(
        SERVICE_TYPE,
        f"{announcement.clique_name}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(host)],
        port=int(port),
        properties={
            "clique": announcement.clique_name,
            "fp": announcement.server_public_key_fingerprint,
            "v": str(announcement.protocol_version),
        },
    )
    zc = Zeroconf()
    await asyncio.get_running_loop().run_in_executor(None, zc.register_service, info)
    return AnnouncementHandle(zc, info)
