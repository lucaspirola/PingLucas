"""Exception hierarchy shared by every PingLucas component."""

from __future__ import annotations


class PingLucasError(Exception):
    """A user-facing PingLucas failure."""


class ConfigError(PingLucasError):
    """The operator configuration is missing or invalid."""


class TransportError(PingLucasError):
    """A watch transport could not deliver or fetch."""


class DeliveryError(PingLucasError):
    """A peer frame could not be queued to a Claude session."""


class RegistryError(PingLucasError):
    """A registry directory or record failed its safety checks."""
