"""Relay routing: agent -> watch, and a wrist-typed answer back again.

The console transport makes the whole path exercisable without a network: pings
land as JSON lines in a file, replies are read from another file.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from _fake_peer import READY_TIMEOUT_S  # noqa: F401  (keeps the helper importable)
from _support import publish_peer
from test_peer_socket import PEER_TOKEN, FakePeer

from ping_lucas.config import Config, state_home
from ping_lucas.daemon import Relay
from ping_lucas.inbox import InboundMessage, Refusal
from ping_lucas.registry import PeerDirectory, PeerSession
from ping_lucas.transports import IncomingReply

SESSION = "33333333-3333-4333-8333-333333333333"


def inbound(
    sender: PeerSession | None,
    body: str,
    *,
    message_id: str = "m-1",
    hop_chain: str = "",
    priority: str = "now",
) -> InboundMessage:
    return InboundMessage(
        message_id=message_id,
        body=body,
        sender=sender,
        sender_address=sender.address if sender else "",
        sender_name=sender.label if sender else "",
        hop_chain=hop_chain,
        mode_attestation="prompting",
        priority=priority,
        received_at=time.time(),
    )


def fake_sender(pid: int = 4242, name: str = "builder") -> PeerSession:
    return PeerSession(
        session_id="peer-session",
        name=name,
        pid=pid,
        proc_start="99",
        socket_path="/tmp/cc-socks-1000/4242.sock",
        registry_dir="/nowhere/sessions",
        record_path="/nowhere/sessions/4242.json",
        cwd="/work/api",
    )


@pytest.fixture
def make_relay(registry_root, runtime_dir, monkeypatch):
    """Build a relay on the console transport and guarantee it is torn down."""
    monkeypatch.setenv("PING_LUCAS_TRANSPORT", "console")
    live: list[Relay] = []

    def build(**overrides) -> Relay:
        config = Config.load()
        config.session_id = SESSION
        for key, value in overrides.items():
            setattr(config, key, value)
        lines: list[str] = []
        built = Relay(config, log=lines.append)
        built.log_lines = lines
        live.append(built)
        return built

    try:
        yield build
    finally:
        for built in live:
            built.shutdown()


@pytest.fixture
def relay(make_relay):
    return make_relay()


def pings(relay: Relay) -> list[dict]:
    path = state_home() / "console" / "pings.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ------------------------------------------------------- agent -> watch


def test_an_inbound_ping_is_tagged_written_out_and_recorded(relay):
    relay._on_ping(inbound(fake_sender(), "should I run the migration?"))

    entries = relay.ledger.pending()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.label == "builder"
    assert entry.preview == "should I run the migration?"
    assert entry.message_id == "m-1"
    assert entry.pid == 4242

    written = pings(relay)
    assert len(written) == 1
    assert written[0]["tag"] == entry.tag
    assert written[0]["body"] == "should I run the migration?"
    assert written[0]["agent"] == "builder"
    assert written[0]["cwd"] == "/work/api"
    assert written[0]["priority"] == "now"

    assert relay.delivered == 1
    # The console transport is bidirectional, so its message id is linked back
    # and a "reply to that notification" gesture can be resolved later.
    assert relay.ledger.get(entry.tag).external_ref == str(written[0]["id"])
    assert relay.ledger.by_external(str(written[0]["id"])).tag == entry.tag


def test_an_unidentified_sender_is_refused_rather_than_silently_dropped(relay):
    with pytest.raises(Refusal):
        relay._on_ping(inbound(None, "who is asking?"))
    assert relay.ledger.pending() == []
    assert pings(relay) == []


def test_a_transport_that_cannot_deliver_produces_a_refusal(relay, monkeypatch):
    def explode(ping):
        raise RuntimeError("the watch is off")

    monkeypatch.setattr(relay.transports[0], "send", explode)
    with pytest.raises(Refusal, match="could not reach the watch"):
        relay._on_ping(inbound(fake_sender(), "anyone home?"))
    assert relay.dropped == 1


# ------------------------------------------------------- reply resolution


def test_an_explicit_tag_routes_to_that_entry(relay):
    first = relay.ledger.record(fake_sender(name="alpha"), "m-1", "first question")
    second = relay.ledger.record(fake_sender(name="beta"), "m-2", "second question")

    entry, body = relay.router.resolve(IncomingReply(text=f"{first.tag} yes"))
    assert entry.tag == first.tag
    assert body == "yes"

    entry, body = relay.router.resolve(IncomingReply(text=f"[{second.tag}] no, hold off"))
    assert entry.tag == second.tag
    assert body == "no, hold off"


def test_an_unknown_tag_is_treated_as_prose_and_keeps_the_whole_text(relay):
    latest = relay.ledger.record(fake_sender(), "m-1", "the only question")

    # "yeah" is four characters drawn entirely from the tag alphabet, so the
    # splitter *will* read it as an address. It must not be eaten.
    text = "yeah go ahead and ship it"
    entry, body = relay.router.resolve(IncomingReply(text=text))

    assert entry.tag == latest.tag
    assert body == text, "the leading word is a false-positive tag, not an address"


def test_an_unknown_bare_tag_still_reaches_the_latest_entry(relay):
    latest = relay.ledger.record(fake_sender(), "m-1", "the only question")
    entry, body = relay.router.resolve(IncomingReply(text="zzzz"))
    assert entry.tag == latest.tag
    assert body == "zzzz"


def test_a_transport_reply_linkage_wins_over_the_latest_entry(relay):
    older = relay.ledger.record(fake_sender(name="alpha"), "m-1", "first")
    relay.ledger.link_external(older.tag, "500")
    relay.ledger.record(fake_sender(name="beta"), "m-2", "second")

    entry, body = relay.router.resolve(IncomingReply(text="do it", reply_to="500"))
    assert entry.tag == older.tag
    assert body == "do it"


def test_with_nothing_pending_a_reply_is_dropped_without_raising(relay):
    assert relay.ledger.pending() == []
    relay.route_reply(IncomingReply(text="hello?"))  # must not raise
    assert relay.replied == 0
    assert any("no agent is waiting" in line for line in relay.log_lines)
    # The operator is told, via the same transport, that it went nowhere.
    assert any("Nobody is waiting on an answer" in ping["body"] for ping in pings(relay))


def test_an_empty_reply_body_is_ignored(relay):
    entry = relay.ledger.record(fake_sender(), "m-1", "question")
    relay.route_reply(IncomingReply(text=f"{entry.tag}"))
    assert relay.replied == 0
    assert not relay.ledger.get(entry.tag).answered


def test_a_reply_to_a_session_that_has_ended_is_reported_not_delivered(relay):
    entry = relay.ledger.record(fake_sender(), "m-1", "question")
    relay.route_reply(IncomingReply(text=f"{entry.tag} yes"))
    assert relay.replied == 0
    assert any("has ended; cannot deliver" in line for line in relay.log_lines)
    assert any("was not delivered" in ping["body"] for ping in pings(relay))


# ------------------------------------------------------------- hop chain


def test_outbound_hop_chain_appends_our_own_token(relay):
    token = relay.inbox.hop_token
    assert len(token) == 24

    assert relay.router.outbound_hop_chain("") == token
    inherited = "a" * 24
    assert relay.router.outbound_hop_chain(inherited) == f"{inherited},{token}"


def test_outbound_hop_chain_caps_at_thirty_two_entries(relay):
    token = relay.inbox.hop_token
    inherited = ",".join(f"{index:024x}" for index in range(40))

    result = relay.router.outbound_hop_chain(inherited)
    hops = result.split(",")
    assert len(hops) == 32
    assert hops[-1] == token
    # The 31 kept ancestors are the most recent ones.
    assert hops[:-1] == [f"{index:024x}" for index in range(9, 40)]


def test_a_router_without_a_hop_token_leaves_the_chain_alone(relay):
    """``pinglucas reply`` borrows a running relay's identity but has no socket."""
    from ping_lucas.router import ReplyRouter

    detached = ReplyRouter(
        ledger=relay.ledger, directory=relay.directory, outbox=relay.outbox, hop_token=""
    )
    assert detached.outbound_hop_chain("a" * 24) == "a" * 24
    assert detached.outbound_hop_chain("") == ""


# ------------------------------------------------------- full round trip


def test_a_tagged_reply_is_delivered_to_the_live_peer(relay, sessions_dir, socket_dir):
    peer_process = FakePeer(socket_dir)
    try:
        publish_peer(sessions_dir, peer_process.pid, peer_process.socket_path, PEER_TOKEN)
        peer = PeerDirectory(self_pid=os.getpid()).by_pid(peer_process.pid)
        assert peer is not None

        relay._on_ping(inbound(peer, "ready to deploy?", hop_chain="b" * 24))
        entry = relay.ledger.pending()[0]

        peer_process.collect_async(collect_s=3.0, expect=2)
        time.sleep(0.1)
        relay.route_reply(IncomingReply(text=f"{entry.tag} yes, go"))
        frames = peer_process.result()["frames"]

        assert [frame.get("type") for frame in frames] == ["auth", "user"]
        assert frames[0]["token"] == PEER_TOKEN
        content = frames[1]["message"]["content"]
        assert f"[{entry.tag}] yes, go" in content
        assert f'hop-chain="{"b" * 24},{relay.inbox.hop_token}"' in content
        assert frames[1]["session_id"] == peer.session_id
        assert frames[1]["from"] == relay.inbox_address

        assert relay.replied == 1
        assert relay.ledger.get(entry.tag).answered is True
    finally:
        peer_process.close()


def test_echo_tag_can_be_switched_off(make_relay, sessions_dir, socket_dir):
    relay = make_relay(echo_tag=False)
    peer_process = FakePeer(socket_dir)
    try:
        publish_peer(sessions_dir, peer_process.pid, peer_process.socket_path, PEER_TOKEN)
        peer = PeerDirectory(self_pid=os.getpid()).by_pid(peer_process.pid)
        relay._on_ping(inbound(peer, "ready?"))
        entry = relay.ledger.pending()[0]

        peer_process.collect_async(collect_s=3.0, expect=2)
        time.sleep(0.1)
        relay.route_reply(IncomingReply(text=f"{entry.tag} plain"))
        frames = peer_process.result()["frames"]
        content = frames[1]["message"]["content"]
        assert "plain" in content
        assert f"[{entry.tag}]" not in content
    finally:
        peer_process.close()


# ------------------------------------------------------------ introspection


def test_stats_reports_the_live_shape_of_the_relay(relay):
    relay._on_ping(inbound(fake_sender(), "one"))
    stats = relay.stats()
    assert stats["pid"] == os.getpid()
    assert stats["session_id"] == SESSION
    assert stats["transports"] == ["console"]
    assert stats["delivered"] == 1
    assert stats["pending"] == 1
    assert stats["address"] == relay.inbox_address
    assert stats["socket"] == str(relay.socket_path)


def test_a_relay_with_no_usable_transport_refuses_to_start(registry_root, runtime_dir, monkeypatch):
    from ping_lucas.errors import PingLucasError

    monkeypatch.setenv("PING_LUCAS_TRANSPORT", "telegram")
    config = Config.load()  # no bot token configured
    with pytest.raises(PingLucasError, match="no usable transport"):
        Relay(config)


def test_shutdown_releases_the_socket_and_the_roster(make_relay, registry_root):
    built = make_relay()
    built.publisher.refresh()
    record = registry_root / "sessions" / f"{os.getpid()}.json"
    assert record.exists()
    assert built.socket_path.exists()

    built.shutdown()
    assert not record.exists()
    assert not built.socket_path.exists()
