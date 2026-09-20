"""Error taxonomy shared by all components."""

from __future__ import annotations


class CliqueError(Exception):
    """Base for all clique errors."""


class ConfigError(CliqueError):
    """Invalid or unreadable configuration."""


class ProtocolError(CliqueError):
    """Malformed message, unknown type, or incompatible version."""


class SignatureError(ProtocolError):
    """Message signature failed verification."""


class NoEligibleNodeError(CliqueError):
    """Router found no node satisfying task constraints."""


class NodeUnavailableError(CliqueError):
    """Target node offline or draining."""


class ModelNotReadyError(CliqueError):
    """Model still downloading/warming on the target node."""


class SessionConflictError(CliqueError):
    """Stale context_version or concurrent migration attempt."""


class LeaseExpiredError(CliqueError):
    """Attempt reported after its lease expired; result must not commit."""
