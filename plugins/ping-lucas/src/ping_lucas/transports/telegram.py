"""Telegram Bot API transport -- the reply-capable default.

Why Telegram for a watch: the iOS app's notifications mirror to the Apple Watch
and expose a reply action there, so Lucas can dictate or scribble an answer
without unlocking his phone. On the server side ``getUpdates`` long-polls, so
no inbound port, no webhook, and no public hostname is needed.

Setup is two values: talk to @BotFather for a token, then message the bot once
so ``pinglucas doctor`` can learn the chat id.
"""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any

from ..errors import ConfigError, TransportError
from . import IncomingReply, OutgoingPing, Transport, register
from ._http import request_json

API = "https://api.telegram.org/bot{token}/{method}"
MAX_TEXT = 3800  # Telegram's hard limit is 4096; leave room for our header.


class TelegramTransport(Transport):
    name = "telegram"
    bidirectional = True

    def __init__(self, settings: dict[str, Any]):
        self.token = str(settings.get("token", "")).strip()
        self.chat_id = str(settings.get("chat_id", "")).strip()
        self.offset_path = Path(settings["offset_path"]) if settings.get("offset_path") else None
        self.silent = bool(settings.get("silent", False))
        if not self.token:
            raise ConfigError("telegram transport needs a bot token (talk to @BotFather)")
        self._offset = self._load_offset()

    # -- setup helpers -----------------------------------------------------

    def _call(self, method: str, payload: dict[str, Any] | None = None, timeout: float = 30.0) -> Any:
        url = API.format(token=self.token, method=method)
        result = request_json(url, method="POST", json_body=payload or {}, timeout=timeout)
        if not isinstance(result, dict) or not result.get("ok"):
            description = (result or {}).get("description", "unknown error")
            raise TransportError(f"telegram {method} failed: {description}")
        return result.get("result")

    def check(self) -> str:
        me = self._call("getMe")
        who = f"@{me.get('username')}" if isinstance(me, dict) else "bot"
        if not self.chat_id:
            return (
                f"{who} reachable, but no chat_id is configured. "
                "Send the bot any message, then run `pinglucas doctor --learn-chat`."
            )
        return f"{who} -> chat {self.chat_id}"

    def discover_chat_id(self) -> str:
        """Learn the chat id from whatever message Lucas last sent the bot."""
        updates = self._call("getUpdates", {"timeout": 0, "limit": 10}) or []
        for update in reversed(updates):
            message = update.get("message") or update.get("channel_post") or {}
            chat = message.get("chat") or {}
            if chat.get("id") is not None:
                return str(chat["id"])
        return ""

    # -- outbound ----------------------------------------------------------

    def send(self, ping: OutgoingPing) -> str:
        if not self.chat_id:
            raise ConfigError("telegram transport has no chat_id; run `pinglucas doctor --learn-chat`")
        payload = {
            "chat_id": self.chat_id,
            "text": self._format(ping),
            "parse_mode": "HTML",
            "disable_notification": self.silent and ping.priority != "now",
            "link_preview_options": {"is_disabled": True},
        }
        result = self._call("sendMessage", payload)
        return str((result or {}).get("message_id", ""))

    def _format(self, ping: OutgoingPing) -> str:
        """Put the answer-critical text first: a watch shows about two lines."""
        body = ping.body.strip()
        if len(body) > MAX_TEXT:
            body = body[: MAX_TEXT - 1] + "…"
        lines = [html.escape(body)]
        footer = []
        if ping.agent:
            footer.append(f"<b>{html.escape(ping.agent)}</b>")
        if ping.cwd:
            footer.append(html.escape(_short_path(ping.cwd)))
        if ping.tag:
            footer.append(f"reply <code>{ping.tag}</code>")
        if footer:
            lines.append("")
            lines.append("<i>" + " · ".join(footer) + "</i>")
        return "\n".join(lines)

    # -- inbound -----------------------------------------------------------

    def poll(self, timeout_s: float = 25.0) -> list[IncomingReply]:
        payload: dict[str, Any] = {"timeout": int(max(0, timeout_s)), "allowed_updates": ["message"]}
        if self._offset:
            payload["offset"] = self._offset
        updates = self._call("getUpdates", payload, timeout=timeout_s + 10.0) or []
        replies: list[IncomingReply] = []
        for update in updates:
            self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
            message = update.get("message") or {}
            chat_id = str((message.get("chat") or {}).get("id", ""))
            # Ignore anyone who is not the configured chat: the bot's username
            # is guessable and strangers can start a conversation with it.
            if self.chat_id and chat_id != self.chat_id:
                continue
            text = message.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            reply_to = (message.get("reply_to_message") or {}).get("message_id", "")
            replies.append(
                IncomingReply(
                    text=text.strip(),
                    reply_to=str(reply_to) if reply_to else "",
                    external_id=str(message.get("message_id", "")),
                    received_at=float(message.get("date", time.time())),
                )
            )
        self._save_offset()
        return replies

    # -- offset persistence ------------------------------------------------

    def _load_offset(self) -> int:
        if self.offset_path is None:
            return 0
        try:
            return int(json.loads(self.offset_path.read_text())["offset"])
        except (OSError, ValueError, KeyError, TypeError):
            return 0

    def _save_offset(self) -> None:
        if self.offset_path is None:
            return
        try:
            self.offset_path.write_text(json.dumps({"offset": self._offset}))
        except OSError:
            pass


def _short_path(value: str) -> str:
    home = str(Path.home())
    return "~" + value[len(home) :] if value.startswith(home) else value


register("telegram", TelegramTransport)
