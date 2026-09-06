"""Watch transports.

No test here touches the network: the HTTP layer is replaced with a recorder,
so what is asserted is the request each transport *would* make and the way it
reads a response back.
"""

from __future__ import annotations

import json

import pytest

from ping_lucas.errors import ConfigError, TransportError
from ping_lucas.transports import IncomingReply, OutgoingPing, Transport, available, build
from ping_lucas.transports.console import ConsoleTransport
from ping_lucas.transports.ntfy import NtfyTransport, _ascii
from ping_lucas.transports.telegram import TelegramTransport
from ping_lucas.transports.webhook import WebhookTransport


def ping(**overrides) -> OutgoingPing:
    fields = {
        "tag": "a3f2",
        "title": "builder",
        "body": "deploy?",
        "agent": "builder",
        "cwd": "/work",
        "priority": "now",
    }
    fields.update(overrides)
    return OutgoingPing(**fields)


# -------------------------------------------------------------- the registry


def test_every_transport_is_registered():
    assert available() == ["console", "ntfy", "telegram", "webhook"]


def test_building_an_unknown_transport_names_the_alternatives():
    with pytest.raises(ConfigError, match="unknown transport 'carrier-pigeon'") as caught:
        build("carrier-pigeon", {})
    assert "console, ntfy, telegram, webhook" in str(caught.value)


def test_each_transport_declares_whether_it_can_carry_a_reply():
    assert ConsoleTransport.bidirectional is True
    assert TelegramTransport.bidirectional is True
    assert NtfyTransport.bidirectional is True
    assert WebhookTransport.bidirectional is False, "a webhook has no reply channel"
    assert Transport.bidirectional is False


# ----------------------------------------------------------------- telegram


@pytest.fixture
def telegram(monkeypatch, tmp_path):
    transport = TelegramTransport(
        {"token": "123:secret", "chat_id": "-99", "offset_path": str(tmp_path / "offset.json")}
    )
    calls: list[tuple[str, dict]] = []
    responses: dict[str, object] = {}

    def fake_call(method, payload=None, timeout=30.0):
        calls.append((method, payload or {}))
        if method in responses:
            return responses[method]
        return {"message_id": 4321}

    monkeypatch.setattr(transport, "_call", fake_call)
    transport.calls = calls
    transport.responses = responses
    return transport


def test_telegram_needs_a_token():
    with pytest.raises(ConfigError, match="bot token"):
        TelegramTransport({})


def test_telegram_format_escapes_html_in_the_body_and_the_agent_name(telegram):
    text = telegram._format(
        ping(body="<b>rm -rf /</b> & then?", agent="<script>alert(1)</script>", cwd="/work")
    )
    assert "<b>rm -rf /</b> & then?" not in text
    assert "&lt;b&gt;rm -rf /&lt;/b&gt; &amp; then?" in text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<script>" not in text
    # Only our own markup survives.
    assert "<b>&lt;script&gt;alert(1)&lt;/script&gt;</b>" in text
    assert "reply <code>a3f2</code>" in text


def test_telegram_format_puts_the_question_first_and_truncates_a_long_body(telegram):
    text = telegram._format(ping(body="z" * 5000))
    assert text.startswith("z")
    assert "…" in text
    assert len(text) < 4096, "Telegram's hard limit"


def test_telegram_send_posts_html_to_the_configured_chat(telegram):
    assert telegram.send(ping()) == "4321"
    method, payload = telegram.calls[-1]
    assert method == "sendMessage"
    assert payload["chat_id"] == "-99"
    assert payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert payload["disable_notification"] is False


def test_telegram_refuses_to_send_without_a_chat_id(tmp_path):
    transport = TelegramTransport({"token": "123:secret"})
    with pytest.raises(ConfigError, match="no chat_id"):
        transport.send(ping())


def test_telegram_poll_ignores_messages_from_another_chat(telegram):
    telegram.responses["getUpdates"] = [
        {
            "update_id": 10,
            "message": {"message_id": 1, "chat": {"id": -99}, "text": "  yes, go  ", "date": 1},
        },
        {
            "update_id": 11,
            "message": {"message_id": 2, "chat": {"id": 12345}, "text": "hello bot", "date": 2},
        },
        {
            "update_id": 12,
            "message": {"message_id": 3, "chat": {"id": "-99"}, "text": "still mine", "date": 3},
        },
    ]
    replies = telegram.poll(1.0)

    assert [reply.text for reply in replies] == ["yes, go", "still mine"]
    assert all(isinstance(reply, IncomingReply) for reply in replies)
    assert replies[0].external_id == "1"
    # The offset advances past *every* update, including the stranger's.
    assert telegram._offset == 13


def test_telegram_poll_keeps_the_reply_linkage(telegram):
    telegram.responses["getUpdates"] = [
        {
            "update_id": 1,
            "message": {
                "message_id": 9,
                "chat": {"id": -99},
                "text": "yes",
                "date": 1,
                "reply_to_message": {"message_id": 4321},
            },
        }
    ]
    reply = telegram.poll(1.0)[0]
    assert reply.reply_to == "4321"
    assert reply.external_id == "9"


def test_telegram_poll_skips_updates_with_no_usable_text(telegram):
    telegram.responses["getUpdates"] = [
        {"update_id": 1, "message": {"chat": {"id": -99}, "sticker": {}}},
        {"update_id": 2, "message": {"chat": {"id": -99}, "text": "   "}},
        {"update_id": 3, "edited_message": {"chat": {"id": -99}, "text": "edited"}},
    ]
    assert telegram.poll(1.0) == []


def test_telegram_persists_its_offset_across_restarts(telegram, tmp_path):
    telegram.responses["getUpdates"] = [
        {"update_id": 77, "message": {"chat": {"id": -99}, "text": "ok", "date": 1}}
    ]
    telegram.poll(1.0)
    assert json.loads((tmp_path / "offset.json").read_text()) == {"offset": 78}

    resumed = TelegramTransport(
        {"token": "123:secret", "chat_id": "-99", "offset_path": str(tmp_path / "offset.json")}
    )
    assert resumed._offset == 78


def test_telegram_reports_an_api_error_as_a_transport_error(monkeypatch, tmp_path):
    import ping_lucas.transports.telegram as module

    transport = TelegramTransport({"token": "123:secret", "chat_id": "-99"})
    monkeypatch.setattr(
        module, "request_json", lambda *a, **k: {"ok": False, "description": "chat not found"}
    )
    with pytest.raises(TransportError, match="chat not found"):
        transport.check()


def test_telegram_discovers_a_chat_id_from_the_most_recent_message(telegram):
    telegram.responses["getUpdates"] = [
        {"message": {"chat": {"id": 111}}},
        {"message": {"chat": {"id": 222}}},
    ]
    assert telegram.discover_chat_id() == "222"
    telegram.responses["getUpdates"] = []
    assert telegram.discover_chat_id() == ""


# --------------------------------------------------------------------- ntfy


def test_ntfy_needs_a_topic():
    with pytest.raises(ConfigError, match="needs a topic"):
        NtfyTransport({})


def test_ntfy_ascii_sanitises_a_header_value():
    assert _ascii("café ☕") == "caf? ?"
    assert _ascii("plain") == "plain"
    # A header value must survive latin-1 encoding, which is what urllib uses.
    assert _ascii("Lucas — builder").encode("latin-1")


def test_ntfy_send_puts_the_sanitised_title_in_a_header(monkeypatch, tmp_path):
    import ping_lucas.transports.ntfy as module

    captured: dict = {}

    def fake_request(url, *, method="GET", data=None, headers=None, timeout=30.0):
        captured.update(url=url, method=method, data=data, headers=headers)
        return b'{"id":"abc123"}'

    monkeypatch.setattr(module, "request", fake_request)
    transport = NtfyTransport({"topic": "t", "reply_topic": "t-reply"})

    assert transport.send(ping(agent="café-builder")) == "abc123"
    assert captured["url"] == "https://ntfy.sh/t"
    assert captured["headers"]["Title"] == "caf?-builder [a3f2]"
    captured["headers"]["Title"].encode("latin-1")  # must not raise
    assert captured["headers"]["Priority"] == "urgent"
    assert captured["data"] == b"deploy?"
    assert "?message=a3f2%20" in captured["headers"]["Actions"]


def test_ntfy_starts_from_now_rather_than_replaying_history(tmp_path):
    transport = NtfyTransport({"topic": "t", "since_path": str(tmp_path / "since.json")})
    assert transport._since.isdigit(), "a timestamp, never the string 'all'"
    assert transport._since != "all"


def test_ntfy_poll_advances_and_persists_its_cursor(monkeypatch, tmp_path):
    import ping_lucas.transports.ntfy as module

    lines = [
        json.dumps({"event": "open"}),
        json.dumps({"event": "message", "id": "m1", "message": " first ", "time": 1}),
        "not json",
        json.dumps({"event": "message", "id": "m2", "message": "", "time": 2}),
        json.dumps({"event": "message", "id": "m3", "message": "second", "time": 3}),
    ]
    monkeypatch.setattr(module, "request", lambda *a, **k: "\n".join(lines).encode())
    since = tmp_path / "since.json"
    transport = NtfyTransport({"topic": "t", "reply_topic": "r", "since_path": str(since)})

    replies = transport.poll(1.0)
    assert [reply.text for reply in replies] == ["first", "second"]
    assert transport._since == "m3"
    assert json.loads(since.read_text()) == {"since": "m3"}


def test_ntfy_without_a_reply_topic_never_polls(monkeypatch):
    import ping_lucas.transports.ntfy as module

    monkeypatch.setattr(module, "request", lambda *a, **k: pytest.fail("must not call out"))
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    assert NtfyTransport({"topic": "t"}).poll(1.0) == []


# ------------------------------------------------------------------ webhook


def test_webhook_needs_a_url():
    with pytest.raises(ConfigError, match="needs a url"):
        WebhookTransport({})


def test_webhook_renders_its_template(monkeypatch):
    import ping_lucas.transports.webhook as module

    captured: dict = {}
    monkeypatch.setattr(
        module, "request", lambda url, **kwargs: captured.update(url=url, **kwargs)
    )
    transport = WebhookTransport({"url": "https://example.invalid/hook"})
    assert transport.send(ping()) == ""
    assert captured["json_body"] == {"title": "builder", "text": "deploy?", "tag": "a3f2"}
    assert captured["method"] == "POST"


# ------------------------------------------------------------------ console


def test_console_roundtrips_a_ping_and_a_reply(tmp_path):
    transport = ConsoleTransport({"dir": str(tmp_path / "console")})
    assert transport.send(ping()) == "1"
    assert transport.send(ping(body="second")) == "2"

    written = [json.loads(line) for line in transport.outbox.read_text().splitlines()]
    assert [item["id"] for item in written] == [1, 2]
    assert written[0]["tag"] == "a3f2"

    transport.inbox.write_text("a3f2 yes\n\nsecond line\n")
    replies = transport.poll(0.0)
    assert [reply.text for reply in replies] == ["a3f2 yes", "second line"]
    assert transport.poll(0.0) == [], "a reply is consumed exactly once"


def test_send_note_uses_the_transport_without_a_tag(tmp_path):
    transport = ConsoleTransport({"dir": str(tmp_path / "console")})
    transport.send_note("the relay restarted")
    written = json.loads(transport.outbox.read_text().splitlines()[0])
    assert written["tag"] == ""
    assert written["body"] == "the relay restarted"
    assert written["agent"] == "relay"
