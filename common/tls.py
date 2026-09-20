"""TLS helpers: a CA bundle that works on machines with no system certs.

Zero-context onboarding reaches the server over public HTTPS (e.g. a
cloudflared tunnel). Many Pythons cannot verify TLS out of the box
(python.org macOS installs without "Install Certificates.command",
minimal containers), which turned into a misleading "server
unreachable" for joiners. certifi ships with our deps (httpx), so use
its bundle whenever the system one fails or is missing.
"""
from __future__ import annotations

import ssl


def client_ssl_context() -> ssl.SSLContext:
    """Verified client context, preferring certifi's CA bundle."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def urlopen_kwargs(url: str) -> dict:
    """Extra kwargs for urllib.request.urlopen: TLS context for https."""
    if url.startswith("https"):
        return {"context": client_ssl_context()}
    return {}
