"""ntfy.sh transport -- zero-signup push, with a second topic for replies.

ntfy needs no account: publish to a topic, subscribe to it on the phone, and
the notification mirrors to the watch. It is the fallback when Lucas does not
want a bot token, at the cost of a clumsier reply: he publishes to a separate
reply topic from the ntfy app rather than replying in place.

Pick unguessable topic names. A topic is a public channel to anyone who knows
its name; ``pinglucas init`` generates random ones for exactly that reason.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from ..errors import ConfigError
from . import IncomingReply, OutgoingPing, Transport, register
from ._http import request, request_json


class NtfyTransport(Transport):
    name = "ntfy"
    bidirectional = True

    def __init__(self, settings: dict[str, Any]):
        self.server = str(settings.get("server", "https://ntfy.sh")).rstrip("/")
        self.topic = str(settings.get("topic", "")).strip()
        self.reply_topic = str(settings.get("reply_topic", "")).strip()
        self.token = str(settings.get("token", "")).strip()
        if not self.topic:
            raise ConfigError("ntfy transport needs a topic")
        self.since_path = Path(settings["since_path"]) if settings.get("since_path") else None
        # Default to "from now on", never "all": a relay restarting with a
        # replayable cursor would re-deliver every reply ntfy still has cached,
        # sending stale answers to agents that moved on hours ago.
        self._since = self._load_since() or str(int(time.time()))
        self._lock = threading.Lock()

    def _load_since(self) -> str:
        if self.since_path is None:
            return ""
        try:
            return str(json.loads(self.since_path.read_text())["since"])
        except (OSError, ValueError, KeyError, TypeError):
            return ""

    def _save_since(self) -> None:
        if self.since_path is None:
            return
        try:
            self.since_path.write_text(json.dumps({"since": self._since}))
        except OSError:
            pass

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def check(self) -> str:
        request(f"{self.server}/{self.topic}/json?poll=1", headers=self._headers(), timeout=10.0)
        if not self.reply_topic:
            return f"{self.server}/{self.topic} (send-only: no reply_topic configured)"
        return f"{self.server}/{self.topic} -> replies on {self.reply_topic}"

    def send(self, ping: OutgoingPing) -> str:
        title = ping.agent or "PingLucas"
        if ping.tag:
            title = f"{title} [{ping.tag}]"
        headers = {
            "Title": _ascii(title),
            "Tags": "speech_balloon",
            "Priority": "urgent" if ping.priority == "now" else "default",
            **self._headers(),
        }
        if self.reply_topic and ping.tag:
            # A tap-through so a reply can be composed with the tag prefilled.
            headers["Actions"] = (
                f"view, Reply, {self.server}/{self.reply_topic}?message={ping.tag}%20"
            )
        body = ping.body.strip()[:3800]
        result = request(
            f"{self.server}/{self.topic}",
            method="POST",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=20.0,
        )
        try:
            return str(json.loads(result.decode("utf-8")).get("id", ""))
        except (UnicodeDecodeError, ValueError):
            return ""

    def poll(self, timeout_s: float = 25.0) -> list[IncomingReply]:
        if not self.reply_topic:
            time.sleep(min(timeout_s, 5.0))
            return []
        url = f"{self.server}/{self.reply_topic}/json?since={self._since}&poll=1"
        raw = request(url, headers=self._headers(), timeout=timeout_s + 5.0)
        replies: list[IncomingReply] = []
        latest = self._since
        for line in raw.decode("utf-8", "replace").splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("event") != "message":
                continue
            text = event.get("message")
            if not isinstance(text, str) or not text.strip():
                continue
            latest = str(event.get("id", latest))
            replies.append(
                IncomingReply(
                    text=text.strip(),
                    external_id=str(event.get("id", "")),
                    received_at=float(event.get("time", time.time())),
                )
            )
        with self._lock:
            # ntfy's ``since`` accepts a message id; advancing it is what stops
            # us replaying the same reply on the next pass.
            if latest != self._since:
                self._since = latest
                self._save_since()
        if not replies:
            # ``poll=1`` returns immediately, so pace the loop ourselves rather
            # than hammering ntfy.sh once per iteration.
            time.sleep(min(max(timeout_s, 0.0), 3.0))
        return replies


def _ascii(value: str) -> str:
    """ntfy header values must be latin-1 safe."""
    return value.encode("ascii", "replace").decode("ascii")


register("ntfy", NtfyTransport)
