"""The relay: one resident process that *is* Lucas, as far as Claude is concerned.

It owns four moving parts:

* a :class:`~ping_lucas.registry.RecordPublisher` that keeps his peer record in
  every Claude session registry on the machine,
* an :class:`~ping_lucas.inbox.Inbox` listening on the Unix socket agents write to,
* a :class:`~ping_lucas.ledger.Ledger` remembering who is waiting for an answer,
* one or more transports pushing to, and reading back from, the watch.

The pid is the identity, so the relay must stay resident. If it dies, its
record stops validating within one Claude refresh and Lucas simply drops off
the roster -- no stale entry pointing at a dead socket.
"""

from __future__ import annotations

import os
import secrets
import signal
import threading
import time
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, state_home
from .errors import PingLucasError, TransportError
from .inbox import Inbox, InboundMessage, Refusal
from .ledger import Ledger, split_tag
from .outbox import Outbox
from .registry import PeerDirectory, RecordPublisher, choose_socket_path
from .router import ReplyRouter
from .safeio import ensure_private_dir
from .transports import IncomingReply, OutgoingPing, Transport, build

POLL_TIMEOUT_S = 25.0
POLL_BACKOFF_MAX_S = 60.0


class Relay:
    """Publishes Lucas as a peer session and shuttles messages to his wrist."""

    def __init__(self, config: Config, *, log=None):
        self.config = config
        self.log = log or (lambda message: None)
        self.session_id = config.ensure_session_id()
        self.token = secrets.token_hex(16)
        self.started_at = time.time()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.delivered = 0
        self.replied = 0
        self.dropped = 0

        ensure_private_dir(state_home())
        self.ledger = Ledger(config.ledger_path, ttl_s=config.ledger_ttl_s)
        self.directory = PeerDirectory(config.extra_claude_roots, self_pid=os.getpid())

        self.transports: list[Transport] = []
        for name in config.transports:
            try:
                self.transports.append(build(name, config.transport_settings(name)))
            except PingLucasError as exc:
                self.log(f"transport {name} unavailable: {exc}")
        if not self.transports:
            raise PingLucasError("no usable transport; run `pinglucas doctor` for details")

        self.socket_path = choose_socket_path(os.getpid())
        self.inbox = Inbox(
            self.socket_path,
            session_id=self.session_id,
            token=self.token,
            directory=self.directory,
            deliver=self._on_ping,
            on_status=self._on_status,
            on_error=self.log,
        )
        self.outbox = Outbox(
            self.directory,
            self_address=self.inbox_address,
            self_name=config.name,
        )
        self.inbox.attach_outbox(self.outbox)
        self.router = ReplyRouter(
            ledger=self.ledger,
            directory=self.directory,
            outbox=self.outbox,
            hop_token=self.inbox.hop_token,
            echo_tag=config.echo_tag,
            mode_attestation=config.mode_attestation,
            log=self.log,
        )

        self.publisher = RecordPublisher(
            session_id=self.session_id,
            name=config.name,
            socket_path=self.socket_path,
            token=self.token,
            cwd=config.cwd or str(Path.home()),
            status="idle",
            extra_roots=config.extra_claude_roots,
            transport=",".join(t.name for t in self.transports),
        )

    @property
    def inbox_address(self) -> str:
        from .registry import uds_address

        return uds_address(str(self.socket_path))

    @property
    def reply_transport(self) -> Transport | None:
        """The transport whose replies we listen to."""
        for transport in self.transports:
            if transport.bidirectional:
                return transport
        return None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        orphans = self.publisher.sweep_orphans()
        if orphans:
            self.log(f"cleared {orphans} record(s) left by a relay that did not shut down cleanly")
        written = self.publisher.refresh()
        self.log(
            f"PingLucas {__version__} is live as '{self.config.name}' "
            f"[{self.session_id[:8]}] in {len(written)} registry root(s)"
        )
        self.log(f"  socket    {self.socket_path}")
        self.log(f"  transport {', '.join(t.name for t in self.transports)}")
        self._spawn(self._publish_loop, "ping-lucas-publisher")
        if self.reply_transport is not None:
            self._spawn(self._reply_loop, "ping-lucas-replies")

    def _spawn(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def run_forever(self) -> None:
        self.start()
        for received in (signal.SIGINT, signal.SIGTERM):
            signal.signal(received, lambda *_: self._stop.set())
        try:
            while not self._stop.wait(1.0):
                pass
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stop.set()
        self.log("withdrawing from the roster")
        try:
            self.publisher.withdraw()
        finally:
            self.inbox.close()
            for transport in self.transports:
                try:
                    transport.close()
                except Exception:
                    pass

    # -- background loops --------------------------------------------------

    def _publish_loop(self) -> None:
        """Re-publish so Claude sessions started after us can still see Lucas.

        The record also carries an honest status: ``busy`` once questions are
        stacking up unanswered, so an agent checking the roster can see he is
        already backed up before adding a ninth thing to his wrist.
        """
        last_heartbeat = time.monotonic()
        while not self._stop.wait(self.config.refresh_interval_s):
            try:
                self.publisher.status = "busy" if self.ledger.pending() else "idle"
                if time.monotonic() - last_heartbeat >= self.config.heartbeat_interval_s:
                    last_heartbeat = time.monotonic()
                self.publisher.refresh()
            except Exception as exc:
                self.log(f"registry refresh failed: {exc}")

    def _reply_loop(self) -> None:
        """Long-poll the watch transport and route whatever Lucas sends back."""
        transport = self.reply_transport
        assert transport is not None
        backoff = 1.0
        while not self._stop.is_set():
            try:
                replies = transport.poll(POLL_TIMEOUT_S)
                backoff = 1.0
            except TransportError as exc:
                self.log(f"{transport.name} poll failed: {exc}; retrying in {backoff:.0f}s")
                self._stop.wait(backoff)
                backoff = min(POLL_BACKOFF_MAX_S, backoff * 2)
                continue
            except Exception as exc:
                self.log(f"{transport.name} poll error: {exc}")
                self._stop.wait(backoff)
                backoff = min(POLL_BACKOFF_MAX_S, backoff * 2)
                continue
            for reply in replies:
                try:
                    self.route_reply(reply)
                except Exception as exc:
                    # Lucas answered and nothing happened. Say so, or he will
                    # assume the agent got it and moved on.
                    self.log(f"could not route reply: {exc}")
                    self._notify(f"Your reply could not be delivered: {exc}")

    # -- agent -> watch ----------------------------------------------------

    def _on_ping(self, message: InboundMessage) -> None:
        """An agent asked Lucas something. Tag it, remember it, push it."""
        if message.sender is None:
            # We cannot route an answer to a peer we could not identify, and a
            # silent dead end is worse than a refusal the agent can see.
            raise Refusal("PingLucas could not verify the sending session's identity")

        entry = self.ledger.record(
            message.sender,
            message.message_id,
            message.body,
            hop_chain=message.hop_chain,
            priority=message.priority,
        )
        ping = OutgoingPing(
            tag=entry.tag,
            title=entry.label,
            body=message.body,
            agent=entry.label,
            cwd=entry.cwd,
            priority=message.priority,
        )

        errors: list[str] = []
        external_ref = ""
        for transport in self.transports:
            try:
                reference = transport.send(ping)
                if transport is self.reply_transport and reference:
                    external_ref = reference
            except Exception as exc:
                errors.append(f"{transport.name}: {exc}")
        if len(errors) == len(self.transports):
            self.dropped += 1
            raise Refusal("PingLucas could not reach the watch: " + "; ".join(errors)[:200])
        if errors:
            self.log("partial delivery -- " + "; ".join(errors))
        if external_ref:
            self.ledger.link_external(entry.tag, external_ref)
        self.delivered += 1
        self.log(f"-> watch [{entry.tag}] from {entry.label}: {entry.preview[:80]}")

    def _on_status(self, sender, frame: dict[str, Any]) -> None:
        status = frame.get("status")
        reason = str(frame.get("reason", ""))[:200]
        label = sender.label if sender is not None else "a peer"
        self.log(f"receipt from {label}: {status} {reason}".rstrip())

    # -- watch -> agent ----------------------------------------------------

    def route_reply(self, reply: IncomingReply) -> None:
        result = self.router.route(reply)
        if result.status == "delivered":
            self.replied += 1
        elif result.status == "no-target":
            self._notify(f"Nobody is waiting on an answer, so I dropped: {reply.text[:80]}")
        elif result.status == "target-gone" and result.entry is not None:
            self._notify(f"[{result.entry.tag}] {result.detail} — your reply was not delivered.")

    def send_to(self, selector: str, message: str, *, priority: str = "now") -> dict[str, Any]:
        """Lucas-initiated message to an agent, addressed by name or id prefix."""
        peer = self.resolve_peer(selector)
        return self.outbox.send_message(
            peer, message, priority=priority, mode_attestation=self.config.mode_attestation
        )

    def resolve_peer(self, selector: str):
        selector = selector.strip()
        peers = self.directory.peers()
        exact = [p for p in peers if p.name.casefold() == selector.casefold()]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise PingLucasError(
                f"{selector!r} is ambiguous: " + ", ".join(p.session_id[:8] for p in exact)
            )
        prefix = [p for p in peers if p.session_id.startswith(selector)] if len(selector) >= 4 else []
        if len(prefix) == 1:
            return prefix[0]
        if len(prefix) > 1:
            raise PingLucasError(f"{selector!r} matches {len(prefix)} sessions; use a longer id")
        raise PingLucasError(f"no live session matches {selector!r}")

    def _notify(self, text: str) -> None:
        for transport in self.transports:
            try:
                transport.send_note(text)
            except Exception:
                pass

    # -- introspection -----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "version": __version__,
            "name": self.config.name,
            "session_id": self.session_id,
            "pid": os.getpid(),
            "socket": str(self.socket_path),
            "address": self.inbox_address,
            "transports": [t.name for t in self.transports],
            "uptime_s": round(time.time() - self.started_at, 1),
            "delivered": self.delivered,
            "replied": self.replied,
            "dropped": self.dropped,
            "pending": len(self.ledger.pending()),
            "live_peers": len(self.directory.peers()),
        }
