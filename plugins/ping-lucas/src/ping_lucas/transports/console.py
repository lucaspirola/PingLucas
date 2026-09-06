"""File-backed transport for development and tests.

Pings are appended to an outbox file; replies are read from an inbox file, one
line per reply. It makes the whole relay exercisable end-to-end without a
network, a phone, or a bot token -- which is how the test suite drives it.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from . import IncomingReply, OutgoingPing, Transport, register


class ConsoleTransport(Transport):
    name = "console"
    bidirectional = True

    def __init__(self, settings: dict[str, Any]):
        root = Path(settings.get("dir", "/tmp")).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        self.outbox = root / str(settings.get("outbox", "pings.jsonl"))
        self.inbox = root / str(settings.get("inbox", "replies.txt"))
        self._consumed = 0
        self._counter = 0

    def check(self) -> str:
        return f"out={self.outbox} in={self.inbox}"

    def send(self, ping: OutgoingPing) -> str:
        self._counter += 1
        with self.outbox.open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(
                    {
                        "id": self._counter,
                        "at": time.time(),
                        "tag": ping.tag,
                        "agent": ping.agent,
                        "cwd": ping.cwd,
                        "priority": ping.priority,
                        "body": ping.body,
                    }
                )
                + "\n"
            )
        return str(self._counter)

    def poll(self, timeout_s: float = 25.0) -> list[IncomingReply]:
        deadline = time.monotonic() + max(0.0, timeout_s)
        while True:
            try:
                lines = self.inbox.read_text(encoding="utf-8").splitlines()
            except OSError:
                lines = []
            fresh = lines[self._consumed :]
            if fresh:
                self._consumed = len(lines)
                return [
                    IncomingReply(text=line.strip(), received_at=time.time())
                    for line in fresh
                    if line.strip()
                ]
            if time.monotonic() >= deadline:
                return []
            time.sleep(0.2)


register("console", ConsoleTransport)
