"""Where PingLucas keeps its settings and state, and how to override them.

One JSON file at ``~/.config/ping-lucas/config.json`` holds everything.  Any
scalar can be overridden by an environment variable, which is what makes the
relay usable from a systemd unit or a container without editing files:

    PING_LUCAS_NAME=lucas
    PING_LUCAS_TRANSPORT=telegram
    PING_LUCAS_TELEGRAM_TOKEN=...
    PING_LUCAS_TELEGRAM_CHAT_ID=...
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .safeio import ensure_private_dir, read_json, write_json

MAX_CONFIG_BYTES = 256 * 1024
DEFAULT_NAME = "lucas"


def config_home() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "ping-lucas"


def state_home() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "ping-lucas"


def config_path() -> Path:
    override = os.environ.get("PING_LUCAS_CONFIG")
    return Path(override) if override else config_home() / "config.json"


@dataclass
class Config:
    """Everything the relay needs to run."""

    #: The name agents see in ``ListAgents`` and address with ``SendMessage``.
    name: str = DEFAULT_NAME
    #: Stable session id, so Lucas keeps one identity across relay restarts.
    session_id: str = ""
    #: Human-readable description shown in the session record's cwd slot.
    cwd: str = ""
    #: Transports to use, in order. The first bidirectional one takes replies.
    transports: list[str] = field(default_factory=lambda: ["console"])
    #: Per-transport settings, keyed by transport name.
    settings: dict[str, Any] = field(default_factory=dict)
    #: Seconds between registry re-publishes (new Claude sessions appear often).
    refresh_interval_s: float = 5.0
    #: Seconds between forced record rewrites, to look alive to any reader.
    heartbeat_interval_s: float = 60.0
    #: How long a question stays answerable by tag.
    ledger_ttl_s: float = 24 * 60 * 60
    #: Extra Claude config roots to publish into beyond the auto-discovered set.
    extra_claude_roots: list[str] = field(default_factory=list)
    #: Prefix every relayed reply with the tag it answered, for agent clarity.
    echo_tag: bool = True
    #: Permission class PingLucas attests when relaying Lucas's words.
    mode_attestation: str = "bypass"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        raw = read_json(path, MAX_CONFIG_BYTES) or {}
        config = cls(
            name=str(raw.get("name", DEFAULT_NAME)),
            session_id=str(raw.get("session_id", "")),
            cwd=str(raw.get("cwd", "")),
            transports=list(raw.get("transports", ["console"])) or ["console"],
            settings=dict(raw.get("settings", {})),
            refresh_interval_s=float(raw.get("refresh_interval_s", 5.0)),
            heartbeat_interval_s=float(raw.get("heartbeat_interval_s", 60.0)),
            ledger_ttl_s=float(raw.get("ledger_ttl_s", 24 * 60 * 60)),
            extra_claude_roots=list(raw.get("extra_claude_roots", [])),
            echo_tag=bool(raw.get("echo_tag", True)),
            mode_attestation=str(raw.get("mode_attestation", "bypass")),
        )
        config.apply_environment()
        config.validate()
        return config

    def apply_environment(self) -> None:
        env = os.environ
        if env.get("PING_LUCAS_NAME"):
            self.name = env["PING_LUCAS_NAME"]
        if env.get("PING_LUCAS_SESSION_ID"):
            self.session_id = env["PING_LUCAS_SESSION_ID"]
        if env.get("PING_LUCAS_TRANSPORT"):
            self.transports = [
                piece.strip() for piece in env["PING_LUCAS_TRANSPORT"].split(",") if piece.strip()
            ]
        for key, (transport, field_name) in _ENV_SETTINGS.items():
            if env.get(key):
                self.settings.setdefault(transport, {})[field_name] = env[key]
        if env.get("PING_LUCAS_CLAUDE_CONFIG_DIRS"):
            # Skip roots we already hold: `init` and `doctor` both load and then
            # save, so appending unconditionally would grow the stored list by
            # one copy of every environment root on each invocation.
            for piece in env["PING_LUCAS_CLAUDE_CONFIG_DIRS"].split(os.pathsep):
                if piece and piece not in self.extra_claude_roots:
                    self.extra_claude_roots.append(piece)

    def validate(self) -> None:
        if not self.name or len(self.name) > 48 or any(ch in self.name for ch in ' \t"<>/\\'):
            raise ConfigError(f"invalid peer name {self.name!r}")
        if self.mode_attestation not in ("bypass", "prompting", ""):
            raise ConfigError("mode_attestation must be 'bypass', 'prompting', or empty")
        if not self.transports:
            raise ConfigError("at least one transport must be configured")
        self.refresh_interval_s = max(1.0, float(self.refresh_interval_s))
        self.heartbeat_interval_s = max(self.refresh_interval_s, float(self.heartbeat_interval_s))

    def ensure_session_id(self) -> str:
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        return self.session_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "session_id": self.session_id,
            "cwd": self.cwd,
            "transports": self.transports,
            "settings": self.settings,
            "refresh_interval_s": self.refresh_interval_s,
            "heartbeat_interval_s": self.heartbeat_interval_s,
            "ledger_ttl_s": self.ledger_ttl_s,
            "extra_claude_roots": self.extra_claude_roots,
            "echo_tag": self.echo_tag,
            "mode_attestation": self.mode_attestation,
        }

    def save(self, path: Path | None = None) -> Path:
        path = path or config_path()
        ensure_private_dir(path.parent)
        write_json(path, self.as_dict(), 0o600)
        return path

    # -- derived paths -----------------------------------------------------

    @property
    def ledger_path(self) -> Path:
        return state_home() / "ledger.json"

    @property
    def offset_path(self) -> Path:
        return state_home() / "telegram-offset.json"

    @property
    def log_path(self) -> Path:
        return state_home() / "relay.log"

    def transport_settings(self, name: str) -> dict[str, Any]:
        """Per-transport settings with the paths the transport needs injected."""
        settings = dict(self.settings.get(name, {}))
        if name == "telegram":
            settings.setdefault("offset_path", str(self.offset_path))
        if name == "ntfy":
            settings.setdefault("since_path", str(state_home() / "ntfy-since.json"))
        if name == "console":
            settings.setdefault("dir", str(state_home() / "console"))
        return settings


_ENV_SETTINGS = {
    "PING_LUCAS_TELEGRAM_TOKEN": ("telegram", "token"),
    "PING_LUCAS_TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
    "PING_LUCAS_NTFY_TOPIC": ("ntfy", "topic"),
    "PING_LUCAS_NTFY_REPLY_TOPIC": ("ntfy", "reply_topic"),
    "PING_LUCAS_NTFY_SERVER": ("ntfy", "server"),
    "PING_LUCAS_NTFY_TOKEN": ("ntfy", "token"),
    "PING_LUCAS_WEBHOOK_URL": ("webhook", "url"),
}
