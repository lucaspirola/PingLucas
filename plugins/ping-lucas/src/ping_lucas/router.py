"""Turning something Lucas typed into a frame the right agent receives.

This is deliberately its own object rather than a method on the relay: the
background relay routes replies arriving from the watch, and ``pinglucas
reply`` routes replies typed at a terminal, and both must behave identically.
Sharing the class is what guarantees that -- a second implementation would
drift the moment either side changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .ledger import Entry, Ledger, split_tag
from .outbox import Outbox
from .registry import PeerDirectory
from .inbox import MAX_HOPS
from .transports import IncomingReply


@dataclass(frozen=True)
class Routed:
    """What happened to one reply."""

    status: str  # delivered | no-target | empty | target-gone
    detail: str = ""
    entry: Entry | None = None
    receipt: dict[str, Any] | None = None


class ReplyRouter:
    def __init__(
        self,
        *,
        ledger: Ledger,
        directory: PeerDirectory,
        outbox: Outbox,
        hop_token: str = "",
        echo_tag: bool = True,
        mode_attestation: str = "bypass",
        log: Callable[[str], None] = lambda message: None,
    ):
        self.ledger = ledger
        self.directory = directory
        self.outbox = outbox
        self.hop_token = hop_token
        self.echo_tag = echo_tag
        self.mode_attestation = mode_attestation
        self.log = log

    def resolve(self, reply: IncomingReply) -> tuple[Entry | None, str]:
        """Decide which waiting agent a wrist-typed reply belongs to.

        Tag first, then the transport's own reply linkage, then the most recent
        question still outstanding -- which is nearly always the one that just
        buzzed.
        """
        tag, body = split_tag(reply.text)
        if tag:
            entry = self.ledger.get(tag)
            if entry is not None:
                return entry, body
            # Four letters at the start of a sentence are far more likely to be
            # a word than a mistyped address, so keep the text whole.
            body = reply.text.strip()
        if reply.reply_to:
            entry = self.ledger.by_external(reply.reply_to)
            if entry is not None:
                return entry, reply.text.strip()
        return self.ledger.latest(), reply.text.strip()

    def route(self, reply: IncomingReply) -> Routed:
        entry, body = self.resolve(reply)
        if entry is None:
            self.log(f"no agent is waiting; ignoring: {reply.text[:60]}")
            return Routed("no-target", "nobody is waiting on an answer")
        if not body:
            self.log(f"empty reply for [{entry.tag}]; ignoring")
            return Routed("empty", "the reply had no text", entry)

        peer = self.directory.refresh(entry.to_peer())
        if peer is None:
            self.log(f"[{entry.tag}] {entry.label} has ended; cannot deliver")
            return Routed("target-gone", f"{entry.label} has ended", entry)

        text = f"[{entry.tag}] {body}" if self.echo_tag else body
        receipt = self.outbox.send_message(
            peer,
            text,
            priority="now",
            hop_chain=self.outbound_hop_chain(entry.hop_chain),
            mode_attestation=self.mode_attestation,
        )
        self.ledger.mark_answered(entry.tag)
        self.log(f"<- {peer.label} [{entry.tag}] {receipt['status']}: {body[:80]}")
        return Routed("delivered", "", entry, receipt)

    def outbound_hop_chain(self, inherited: str) -> str:
        """Append our hop token when answering a peer turn.

        Claude distinguishes an absent hop chain -- a fresh, human-driven turn
        -- from a present one carried along a peer route. A reply to an agent's
        question is the latter, so the chain grows, which is what lets both
        ends detect a loop.

        The cap is the inbox's own inbound limit, not the wire maximum: a relay
        that emitted a longer chain than it accepts would refuse its own
        traffic if two relays ever sat on the same route.
        """
        if not self.hop_token:
            return inherited
        hops = inherited.split(",") if inherited else []
        return ",".join((*hops, self.hop_token)[-MAX_HOPS:])
