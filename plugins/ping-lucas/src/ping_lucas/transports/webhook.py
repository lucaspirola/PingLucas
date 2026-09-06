"""Generic webhook transport -- for Pushcut, Apple Shortcuts, Slack, or a script.

Send-only by design. Point it at any URL that produces a notification on the
watch; pair it with a bidirectional transport if replies matter.
"""

from __future__ import annotations

from typing import Any

from ..errors import ConfigError
from . import OutgoingPing, Transport, register
from ._http import request


class WebhookTransport(Transport):
    name = "webhook"
    bidirectional = False

    def __init__(self, settings: dict[str, Any]):
        self.url = str(settings.get("url", "")).strip()
        self.method = str(settings.get("method", "POST")).upper()
        self.headers = dict(settings.get("headers", {}))
        self.template = settings.get("template") or {
            "title": "{agent}",
            "text": "{body}",
            "tag": "{tag}",
        }
        if not self.url:
            raise ConfigError("webhook transport needs a url")

    def check(self) -> str:
        return f"{self.method} {self.url}"

    def send(self, ping: OutgoingPing) -> str:
        fields = {
            "tag": ping.tag,
            "title": ping.title,
            "body": ping.body,
            "agent": ping.agent,
            "cwd": ping.cwd,
            "priority": ping.priority,
        }
        payload = {
            key: (value.format(**fields) if isinstance(value, str) else value)
            for key, value in self.template.items()
        }
        request(self.url, method=self.method, json_body=payload, headers=self.headers, timeout=20.0)
        return ""


register("webhook", WebhookTransport)
