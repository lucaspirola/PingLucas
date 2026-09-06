"""Inbox + Outbox end to end over a real AF_UNIX socket.

The peer here is a genuine second process (see ``_fake_peer``), because the
checks under test are kernel-level ones: ``SO_PEERCRED`` must report a pid that
is *not* ours, and that pid must be the one the registry record claims.  A
thread would silently pass every one of these tests for the wrong reason.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

import _fake_peer
from _support import publish_peer
from ping_lucas.envelope import build_envelope, user_frame
from ping_lucas.inbox import GUARD_CAPACITY, MAX_HOPS, MAX_SELF_HOPS, Inbox, InboundMessage, Refusal
from ping_lucas.outbox import Outbox
from ping_lucas.registry import PeerDirectory, uds_address

RELAY_SESSION = "11111111-1111-4111-8111-111111111111"
PEER_TOKEN = "abcdefabcdefabcdefabcdefabcdefab"
RELAY_TOKEN = "0123456789abcdef0123456789abcdef"


def receipts(result: dict) -> list[dict]:
    """The status frames the relay sent back, minus its own auth line."""
    return [frame for frame in result["frames"] if frame.get("type") != "auth"]


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakePeer:
    """Parent-side handle on the spawned fake Claude peer."""

    def __init__(self, socket_dir):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self._connection = parent
        self._process = context.Process(
            target=_fake_peer.peer_main,
            args=(child, str(socket_dir), PEER_TOKEN),
            name="fake-claude-peer",
            daemon=True,
        )
        self._process.start()
        child.close()
        if not parent.poll(_fake_peer.READY_TIMEOUT_S):
            self.close(force=True)
            raise AssertionError("the fake peer never came up")
        self.ready = parent.recv()

    pid = property(lambda self: int(self.ready["pid"]))
    socket_path = property(lambda self: str(self.ready["socket_path"]))
    address = property(lambda self: uds_address(str(self.ready["socket_path"])))

    def request(self, **command) -> None:
        self._connection.send(command)

    def result(self, timeout: float = 30.0) -> dict:
        if not self._connection.poll(timeout):
            raise AssertionError("the fake peer stopped responding")
        return self._connection.recv()

    def send(
        self, target_socket, token, frames, *, collect_s=0.0, expect=1, chunk=8, auth=True
    ) -> dict:
        self.request(
            op="send",
            target_socket=str(target_socket),
            target_token=token,
            frames=list(frames),
            chunk=chunk,
            collect_s=collect_s,
            expect=expect,
            auth=auth,
        )
        return self.result()

    def collect_async(self, *, collect_s: float, expect: int = 1) -> None:
        """Start listening for callbacks without blocking the parent."""
        self.request(op="collect", collect_s=collect_s, expect=expect)

    def close(self, *, force: bool = False) -> None:
        if self._process.is_alive() and not force:
            try:
                self._connection.send({"op": "stop"})
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._process.join(timeout=5)
        if self._process.is_alive():
            self._process.terminate()
            self._process.join(timeout=5)
        try:
            self._connection.close()
        except OSError:
            pass


@dataclass
class Harness:
    inbox: Inbox
    outbox: Outbox
    peer: FakePeer
    delivered: list[InboundMessage]
    errors: list[str]
    policy: dict

    @property
    def token(self) -> str:
        return self.inbox.token

    def frame(
        self,
        text: str = "is the migration safe to run?",
        *,
        message_id: str = "",
        from_address: str | None = None,
        hop_chain: str = "",
        priority: str = "now",
        mode: str = "prompting",
        session_id: str | None = RELAY_SESSION,
    ) -> dict[str, Any]:
        address = self.peer.address if from_address is None else from_address
        content = build_envelope(
            text,
            from_address=address,
            from_name="claude-echo",
            hop_chain=hop_chain,
            mode_attestation=mode,
            provenance="",
        )
        built = user_frame(
            message_id=message_id or str(uuid.uuid4()),
            session_id=session_id or "",
            content=content,
            from_address=address,
            priority=priority,
            mode_attestation=mode,
        )
        if session_id is None:
            built.pop("session_id")
        return built

    def deliver_to(self, frames, **kwargs) -> dict:
        return self.peer.send(self.inbox.socket_path, self.token, frames, **kwargs)


@pytest.fixture
def harness(sessions_dir, socket_dir):
    peer = FakePeer(socket_dir)
    publish_peer(sessions_dir, peer.pid, peer.socket_path, PEER_TOKEN)

    delivered: list[InboundMessage] = []
    errors: list[str] = []
    policy: dict = {"refuse": None}

    def deliver(message: InboundMessage) -> None:
        # Mirrors the daemon: a peer we cannot address is refused, not queued.
        if message.sender is None:
            raise Refusal("PingLucas could not verify the sending session's identity")
        if policy["refuse"] is not None:
            raise Refusal(policy["refuse"])
        delivered.append(message)

    directory = PeerDirectory(self_pid=os.getpid())
    inbox = Inbox(
        socket_dir / f"relay-{os.getpid()}.sock",
        session_id=RELAY_SESSION,
        token=RELAY_TOKEN,
        directory=directory,
        deliver=deliver,
        on_error=errors.append,
    )
    outbox = Outbox(
        directory, self_address=uds_address(str(inbox.socket_path)), self_name="lucas"
    )
    inbox.attach_outbox(outbox)
    try:
        yield Harness(inbox, outbox, peer, delivered, errors, policy)
    finally:
        peer.close()
        inbox.close()
    assert not inbox.socket_path.exists(), "the inbox must clean up its socket"


# ------------------------------------------------------------- the happy path


def test_an_authenticated_frame_reaches_deliver_with_an_identified_sender(harness):
    result = harness.deliver_to([harness.frame("deploy or wait?")], collect_s=0.4, expect=1)

    assert result["sent"] == 1
    assert wait_for(lambda: len(harness.delivered) == 1)
    message = harness.delivered[0]
    assert message.body == "deploy or wait?"
    assert message.sender is not None
    assert message.sender.pid == harness.peer.pid != os.getpid()
    assert message.sender.socket_path == harness.peer.socket_path
    assert message.sender_address == harness.peer.address
    assert message.sender_name == "claude-echo"
    assert message.mode_attestation == "prompting"
    assert message.priority == "now"
    assert result["frames"] == [], "a delivered message gets no failure receipt"


def test_the_provenance_block_survives_the_wire(harness):
    from ping_lucas.envelope import PROVENANCE

    content = build_envelope(
        "ready?", from_address=harness.peer.address, from_name="claude-echo"
    )
    frame = user_frame(
        message_id="prov-1",
        session_id=RELAY_SESSION,
        content=content,
        from_address=harness.peer.address,
    )
    harness.deliver_to([frame])
    assert wait_for(lambda: len(harness.delivered) == 1)
    assert harness.delivered[0].body == PROVENANCE + "\n\nready?"


# ---------------------------------------------------------------- auth & bind


def test_a_wrong_token_delivers_nothing(harness):
    harness.peer.send(harness.inbox.socket_path, "f" * 32, [harness.frame("secret")], collect_s=0.3)
    time.sleep(0.3)
    assert harness.delivered == []


def test_a_frame_sent_without_any_auth_line_delivers_nothing(harness):
    harness.peer.send(
        harness.inbox.socket_path, None, [harness.frame("secret")], collect_s=0.3, auth=False
    )
    time.sleep(0.3)
    assert harness.delivered == []


def test_a_non_string_token_delivers_nothing(harness):
    harness.peer.send(harness.inbox.socket_path, None, [harness.frame("secret")], collect_s=0.3)
    time.sleep(0.3)
    assert harness.delivered == []


def test_a_sender_with_no_registry_record_is_refused(harness, sessions_dir):
    (sessions_dir / f"{harness.peer.pid}.json").unlink()

    result = harness.deliver_to([harness.frame("who am i?")], collect_s=0.5)
    time.sleep(0.3)
    assert harness.delivered == []
    assert result["frames"] == [], "an unidentifiable sender cannot be sent a receipt either"
    assert any("refused" in line for line in harness.errors)


def test_a_from_address_that_does_not_match_the_connecting_pid_is_rejected(harness, socket_dir):
    impostor = uds_address(str(socket_dir / "99999.sock"))
    assert impostor != harness.peer.address

    harness.deliver_to([harness.frame("let me in", from_address=impostor)], collect_s=0.4)
    time.sleep(0.3)
    assert harness.delivered == []
    assert any("refused" in line for line in harness.errors)


def test_a_frame_for_another_session_id_is_ignored(harness):
    harness.deliver_to([harness.frame("wrong door", session_id="some-other-session")])
    time.sleep(0.3)
    assert harness.delivered == []


def test_a_frame_with_an_unparseable_envelope_is_ignored(harness):
    frame = user_frame(
        message_id="bad-1",
        session_id=RELAY_SESSION,
        content="just some text, no envelope",
        from_address=harness.peer.address,
    )
    harness.deliver_to([frame])
    time.sleep(0.3)
    assert harness.delivered == []


def test_a_frame_with_an_invalid_priority_is_ignored(harness):
    frame = harness.frame("soon please")
    frame["priority"] = "eventually"
    harness.deliver_to([frame])
    time.sleep(0.3)
    assert harness.delivered == []


# --------------------------------------------------------------------- guards


def test_the_rate_limiter_drops_the_thirty_first_rapid_message(harness):
    assert GUARD_CAPACITY == 30
    frames = [harness.frame(f"question number {index}") for index in range(31)]

    result = harness.deliver_to(frames, chunk=8, collect_s=2.0, expect=2)
    assert result["sent"] == 31

    assert wait_for(lambda: len(harness.delivered) == 30)
    time.sleep(0.2)
    assert len(harness.delivered) == 30, "the 31st must not have been queued"

    status = receipts(result)
    assert len(status) == 1, "exactly one message may be over the limit"
    receipt = status[0]
    assert receipt["type"] == "control"
    assert receipt["action"] == "peer_message_status"
    assert receipt["status"] == "dropped"
    assert receipt["drop_reason"] == "rate-limited"
    assert receipt["from"] == uds_address(str(harness.inbox.socket_path))
    # The relay reads the four connections concurrently, so *which* message
    # loses the race is not fixed -- but it must be one of ours, and it must
    # be the one that never reached the callback.
    dropped = receipt["orig_msg_id"]
    assert receipt["dropped_msg_ids"] == [dropped]
    assert dropped in {frame["msg_id"] for frame in frames}
    assert dropped not in {message.message_id for message in harness.delivered}


def test_a_repeated_message_id_is_a_duplicate(harness):
    first = harness.frame("run the migration", message_id="same-id")
    second = harness.frame("something else entirely", message_id="same-id")

    result = harness.deliver_to([first, second], collect_s=1.0, expect=2)
    assert wait_for(lambda: len(harness.delivered) == 1)
    time.sleep(0.2)
    assert len(harness.delivered) == 1

    assert len(receipts(result)) == 1
    assert receipts(result)[0]["drop_reason"] == "duplicate"
    assert receipts(result)[0]["orig_msg_id"] == "same-id"


def test_an_immediately_repeated_body_is_a_duplicate_but_a_b_a_is_not(harness):
    repeated = harness.deliver_to(
        [harness.frame("status?"), harness.frame("status?")], collect_s=1.0, expect=2
    )
    assert wait_for(lambda: len(harness.delivered) == 1)
    time.sleep(0.2)
    assert len(harness.delivered) == 1
    assert [f["drop_reason"] for f in receipts(repeated)] == ["duplicate"]

    harness.delivered.clear()
    alternating = harness.deliver_to(
        [harness.frame("A"), harness.frame("B"), harness.frame("A")], collect_s=0.5, expect=0
    )
    assert wait_for(lambda: len(harness.delivered) == 3)
    assert [m.body for m in harness.delivered] == ["A", "B", "A"]
    assert receipts(alternating) == []


def test_a_runaway_hop_chain_is_dropped(harness):
    assert MAX_HOPS == 28
    chain = ",".join(f"{index:024x}" for index in range(29))

    result = harness.deliver_to(
        [harness.frame("looping?", hop_chain=chain)], collect_s=1.0, expect=2
    )
    time.sleep(0.2)
    assert harness.delivered == []
    assert len(receipts(result)) == 1
    assert receipts(result)[0]["drop_reason"] == "hop-runaway"
    assert receipts(result)[0]["status"] == "dropped"


def test_a_hop_chain_at_the_limit_is_still_accepted(harness):
    chain = ",".join(f"{index:024x}" for index in range(MAX_HOPS))
    harness.deliver_to([harness.frame("still fine", hop_chain=chain)], collect_s=0.3, expect=0)
    assert wait_for(lambda: len(harness.delivered) == 1)
    assert harness.delivered[0].hop_chain == chain


def test_our_own_hop_token_repeated_ten_times_is_a_loop(harness):
    assert MAX_SELF_HOPS == 10
    chain = ",".join([harness.inbox.hop_token] * 10)

    result = harness.deliver_to(
        [harness.frame("round and round", hop_chain=chain)], collect_s=1.0, expect=2
    )
    time.sleep(0.2)
    assert harness.delivered == []
    assert len(receipts(result)) == 1
    assert receipts(result)[0]["drop_reason"] == "hop-loop"


def test_nine_of_our_own_hops_are_still_allowed(harness):
    chain = ",".join([harness.inbox.hop_token] * 9)
    harness.deliver_to([harness.frame("nearly", hop_chain=chain)], collect_s=0.3, expect=0)
    assert wait_for(lambda: len(harness.delivered) == 1)


# -------------------------------------------------------------------- refusal


def test_a_refusal_produces_an_expired_receipt_marked_refused(harness):
    harness.policy["refuse"] = "PingLucas could not reach the watch"

    result = harness.deliver_to(
        [harness.frame("urgent", message_id="ref-1")], collect_s=1.5, expect=2
    )
    assert harness.delivered == []
    assert len(receipts(result)) == 1
    receipt = receipts(result)[0]
    assert receipt["msgV"] == 1
    assert receipt["type"] == "control"
    assert receipt["action"] == "peer_message_status"
    assert receipt["status"] == "expired"
    assert receipt["status_detail"] == "refused"
    assert receipt["orig_msg_id"] == "ref-1"
    assert "PingLucas could not reach the watch" in receipt["reason"]
    assert "drop_reason" not in receipt


def test_the_receipt_is_written_by_this_process_to_the_peers_own_socket(harness):
    harness.policy["refuse"] = "no"
    result = harness.deliver_to([harness.frame("x", message_id="ref-2")], collect_s=1.5, expect=2)
    assert result["callers"] == [os.getpid()]


# --------------------------------------------------------------------- outbox


def test_the_outbox_delivers_a_message_to_the_live_peer(harness):
    peer = PeerDirectory(self_pid=os.getpid()).by_pid(harness.peer.pid)
    assert peer is not None

    harness.peer.collect_async(collect_s=3.0, expect=2)
    time.sleep(0.1)
    receipt = harness.outbox.send_message(
        peer, "yes, go ahead", priority="next", message_id="out-1"
    )
    frames = harness.peer.result()["frames"]
    assert [f.get("type") for f in frames] == ["auth", "user"]
    assert frames[0]["token"] == PEER_TOKEN, "the outbox must present the peer's own key"
    assert frames[1]["msg_id"] == "out-1"
    assert frames[1]["session_id"] == peer.session_id
    assert frames[1]["priority"] == "next"
    assert frames[1]["from"] == uds_address(str(harness.inbox.socket_path))
    assert "yes, go ahead" in frames[1]["message"]["content"]
    assert receipt["status"] == "queued"
    assert receipt["target_session"] == peer.session_id


def test_the_outbox_refuses_a_peer_whose_record_has_gone(harness, sessions_dir):
    from ping_lucas.errors import DeliveryError

    peer = PeerDirectory(self_pid=os.getpid()).by_pid(harness.peer.pid)
    (sessions_dir / f"{harness.peer.pid}.json").unlink()
    with pytest.raises(DeliveryError):
        harness.outbox.send_message(peer, "nobody home")


def test_a_second_inbox_cannot_evict_a_live_one(harness):
    from ping_lucas.errors import PingLucasError

    with pytest.raises(PingLucasError, match="a live relay already owns"):
        Inbox(
            harness.inbox.socket_path,
            session_id=RELAY_SESSION,
            token=RELAY_TOKEN,
            directory=harness.inbox.directory,
            deliver=lambda message: None,
        )


def test_json_that_is_not_a_frame_is_survivable(harness):
    junk = [{"msgV": 2, "type": "user"}, {"type": "user"}, harness.frame("after the junk")]
    harness.deliver_to(junk, collect_s=0.3, expect=0)
    assert wait_for(lambda: len(harness.delivered) == 1)
    assert harness.delivered[0].body == "after the junk"
    assert json.dumps(junk)  # the junk really was valid JSON, just not a valid frame
