"""Watch transports: the last hop between an agent and Lucas's wrist.

A transport does two things -- push a notification out, and yield the replies
that come back.  Everything above this layer is transport-agnostic, so adding
a new channel means adding one file here and nothing else.

The reply object carries an optional ``reply_to`` so a channel that knows which
message was answered (Telegram does) can route precisely, while a channel that
does not simply falls back to the ledger's tag parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable

from ..errors import ConfigError


@dataclass(frozen=True)
class OutgoingPing:
    """A question on its way to the watch."""

    tag: str
    title: str
    body: str
    agent: str
    cwd: str = ""
    priority: str = "now"


@dataclass(frozen=True)
class IncomingReply:
    """Something Lucas said back."""

    text: str
    reply_to: str = ""
    external_id: str = ""
    received_at: float = 0.0


class Transport:
    """Base class. Subclasses override :meth:`send` and :meth:`poll`."""

    name = "base"
    #: True when the channel can carry a free-text reply from the watch.
    bidirectional = False

    def describe(self) -> str:
        return self.name

    def check(self) -> str:
        """Validate credentials. Returns a human-readable status line."""
        return "ok"

    def send(self, ping: OutgoingPing) -> str:
        """Deliver a ping; return a transport-side id usable as ``reply_to``."""
        raise NotImplementedError

    def send_note(self, text: str) -> str:
        """Deliver a plain operational note (no agent is waiting on it)."""
        return self.send(OutgoingPing(tag="", title="PingLucas", body=text, agent="relay"))

    def poll(self, timeout_s: float = 25.0) -> list[IncomingReply]:
        """Block for up to ``timeout_s`` and return any replies received."""
        return []

    def close(self) -> None:
        return None


_BUILDERS: dict[str, Callable[[dict[str, Any]], Transport]] = {}


def register(name: str, builder: Callable[[dict[str, Any]], Transport]) -> None:
    _BUILDERS[name] = builder


def available() -> list[str]:
    return sorted(_BUILDERS)


def build(name: str, settings: dict[str, Any]) -> Transport:
    if name not in _BUILDERS:
        raise ConfigError(f"unknown transport {name!r}; available: {', '.join(available())}")
    return _BUILDERS[name](settings)


def build_all(names: Iterable[str], settings: dict[str, Any]) -> list[Transport]:
    return [build(name, settings.get(name, {})) for name in names]


# Importing the concrete transports registers them.
from . import console, ntfy, telegram, webhook  # noqa: E402,F401
