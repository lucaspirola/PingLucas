"""Inbound half of the peer protocol: the socket agents write Lucas's pings to.

This is a small newline-delimited-JSON server on an AF_UNIX socket.  A caller
must be the same Unix user, must present the bearer token from our key file,
and must send a canonically-formed frame.  Everything that survives those
checks is handed to a ``deliver`` callback -- in the daemon, that callback is
what puts the message on Lucas's wrist.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .envelope import VALID_MODE_ATTESTATIONS, VALID_PRIORITIES, parse_envelope, valid_hop_chain
from .errors import PingLucasError
from .outbox import peer_credentials
from .registry import PeerSession, parse_uds_address

MAX_LINE_BYTES = 1024 * 1024
MAX_FRAMES_PER_CONNECTION = 8
MAX_CONNECTIONS = 64
MAX_DELIVERY_SLOTS = 50
MAX_GUARDED_SENDERS = 256
MAX_HOPS = 28
MAX_SELF_HOPS = 10

GUARD_CAPACITY = 30.0
GUARD_REFILL_PER_SECOND = 0.5
DEDUP_SECONDS = 30.0

VALID_STATUSES = frozenset(("held", "delivered", "denied", "expired", "refused", "dropped"))


class Drop(PingLucasError):
    """A verified message PingLucas deliberately did not enqueue."""

    def __init__(self, reason: str, detail: str):
        self.reason = reason
        super().__init__(detail)


class Refusal(PingLucasError):
    """A verified message refused by policy."""


@dataclass(frozen=True)
class InboundMessage:
    """One ping from an agent, already authenticated and unwrapped."""

    message_id: str
    body: str
    sender: PeerSession | None
    sender_address: str
    sender_name: str
    hop_chain: str
    mode_attestation: str
    priority: str
    received_at: float

    @property
    def sender_label(self) -> str:
        if self.sender is not None:
            return self.sender.label
        return self.sender_name or "unaddressable-peer"


class Inbox:
    """Owns the Unix socket and turns authenticated frames into callbacks."""

    def __init__(
        self,
        socket_path: Path,
        *,
        session_id: str,
        token: str,
        directory,
        deliver: Callable[[InboundMessage], None],
        on_status: Callable[[PeerSession | None, dict[str, Any]], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        self.socket_path = Path(socket_path)
        self.session_id = session_id
        self.token = token
        self.directory = directory
        self._deliver = deliver
        self._on_status = on_status
        self._on_error = on_error or (lambda message: None)

        self.hop_token = secrets.token_hex(12)
        self._closed = threading.Event()
        self._slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._delivery_slots = threading.BoundedSemaphore(MAX_DELIVERY_SLOTS)
        self._guards_lock = threading.Lock()
        self._guards: dict[str, dict[str, Any]] = {}

        self._prepare_socket_path()
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._server.bind(str(self.socket_path))
            os.chmod(self.socket_path, 0o600)
            self._server.listen(MAX_CONNECTIONS)
            self._server.settimeout(0.5)
            self._inode = self.socket_path.lstat().st_ino
        except Exception:
            self._server.close()
            raise
        self._thread = threading.Thread(target=self._accept_loop, name="ping-lucas-inbox", daemon=True)
        self._thread.start()

    # -- lifecycle ---------------------------------------------------------

    def _prepare_socket_path(self) -> None:
        """Claim the socket path, refusing to evict a relay that is still live."""
        from .safeio import ensure_private_dir

        ensure_private_dir(self.socket_path.parent)
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
            raise PingLucasError(f"refusing to replace a non-owned socket: {self.socket_path}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.1)
        try:
            probe.connect(str(self.socket_path))
        except OSError:
            pass  # Nobody is listening: a leftover from an unclean exit.
        else:
            raise PingLucasError(f"a live relay already owns {self.socket_path}")
        finally:
            probe.close()
        self.socket_path.unlink()

    def close(self) -> None:
        self._closed.set()
        try:
            self._server.close()
        except OSError:
            pass
        # Only unlink the socket if it is still the one we bound: a successor
        # relay may already have replaced it.
        try:
            if self.socket_path.lstat().st_ino == self._inode:
                self.socket_path.unlink()
        except OSError:
            pass

    # -- accept ------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._closed.is_set():
            try:
                connection, _ = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            if not self._slots.acquire(blocking=False):
                connection.close()
                continue
            threading.Thread(
                target=self._serve, args=(connection,), name="ping-lucas-frame", daemon=True
            ).start()

    def _serve(self, connection: socket.socket) -> None:
        try:
            self._handle(connection)
        except Exception as exc:  # never let one bad frame kill the relay
            self._on_error(f"inbox connection failed: {exc}")
        finally:
            self._slots.release()
            connection.close()

    def _handle(self, connection: socket.socket) -> None:
        peer_pid, peer_uid, _ = peer_credentials(connection)
        if peer_uid != os.getuid():
            return
        connection.settimeout(30.0)
        with connection.makefile("rb") as stream:
            auth_line = stream.readline(MAX_LINE_BYTES + 1)
            if not auth_line or len(auth_line) > MAX_LINE_BYTES:
                return
            try:
                auth = json.loads(auth_line.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                return
            if not isinstance(auth, dict) or auth.get("type") != "auth":
                return
            supplied = auth.get("token")
            if not isinstance(supplied, str) or not secrets.compare_digest(supplied, self.token):
                return
            for _ in range(MAX_FRAMES_PER_CONNECTION):
                line = stream.readline(MAX_LINE_BYTES + 1)
                if not line or len(line) > MAX_LINE_BYTES:
                    return
                self._process(peer_pid, line)

    # -- frames ------------------------------------------------------------

    def _process(self, peer_pid: int, line: bytes) -> None:
        try:
            frame = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return
        if not isinstance(frame, dict) or frame.get("msgV") != 1:
            return
        if frame.get("type") == "control":
            self._process_status(peer_pid, frame)
            return
        if frame.get("type") != "user":
            return

        message_id = frame.get("msg_id")
        outer_from = frame.get("from", "")
        session_id = frame.get("session_id")
        message = frame.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if (
            not isinstance(message_id, str)
            or not message_id
            or len(message_id) > 128
            or not isinstance(outer_from, str)
            or not isinstance(content, str)
            or (isinstance(message, dict) and message.get("role") != "user")
            or (session_id is not None and session_id != self.session_id)
            or frame.get("priority") not in VALID_PRIORITIES
        ):
            return

        parsed = parse_envelope(content)
        if parsed is None:
            return
        attrs, body = parsed
        if outer_from and attrs.get("from", "") != outer_from:
            return

        sender = self._identify(peer_pid, outer_from) if outer_from else None

        hop_chain = attrs.get("hop-chain", "")
        # A fresh, user-driven SendMessage carries no hop-chain at all. That is
        # legitimately different from a present-but-malformed one.
        if not valid_hop_chain(hop_chain, optional=True):
            self._fail(sender, message_id, "hop-runaway", "malformed hop chain")
            return
        hops = hop_chain.split(",") if hop_chain else []
        if len(hops) > MAX_HOPS:
            self._fail(sender, message_id, "hop-runaway", f"hop chain exceeds {MAX_HOPS} hops")
            return
        if hops.count(self.hop_token) >= MAX_SELF_HOPS:
            self._fail(sender, message_id, "hop-loop", "message looped back through this relay")
            return

        mode = attrs.get("from-mode", "")
        if mode not in VALID_MODE_ATTESTATIONS:
            mode = ""

        verdict = self._guard(str(peer_pid), message_id, body)
        if verdict:
            self._fail(sender, message_id, verdict, f"message {verdict}")
            return

        if not self._delivery_slots.acquire(blocking=False):
            self._fail(sender, message_id, "queue-full", "the relay queue is full")
            return
        try:
            self._deliver(
                InboundMessage(
                    message_id=message_id,
                    body=body,
                    sender=sender,
                    sender_address=outer_from,
                    sender_name=attrs.get("from-name", ""),
                    hop_chain=hop_chain,
                    mode_attestation=mode,
                    priority=str(frame["priority"]),
                    received_at=time.time(),
                )
            )
        except Drop as exc:
            self._fail(sender, message_id, exc.reason, str(exc))
        except Refusal as exc:
            self._refuse(sender, message_id, str(exc))
        except Exception as exc:
            self._fail(sender, message_id, "queue-full", str(exc))
        finally:
            self._delivery_slots.release()

    def _process_status(self, peer_pid: int, frame: dict[str, Any]) -> None:
        if frame.get("action") != "peer_message_status" or self._on_status is None:
            return
        status = frame.get("status")
        if status not in VALID_STATUSES:
            return
        outer_from = frame.get("from", "")
        sender = self._identify(peer_pid, outer_from) if isinstance(outer_from, str) else None
        self._on_status(sender, frame)

    def _identify(self, peer_pid: int, outer_from: str) -> PeerSession | None:
        """Bind a claimed ``uds:`` address to the pid that actually connected."""
        peer = self.directory.by_pid(peer_pid)
        if peer is None:
            return None
        if not outer_from:
            return peer
        claimed = parse_uds_address(outer_from)
        if not claimed or claimed != peer.socket_path:
            return None
        return peer

    # -- guards ------------------------------------------------------------

    def _guard(self, sender_key: str, message_id: str, body: str) -> str:
        """Token-bucket rate limit plus replay and immediate-duplicate rejection."""
        now = time.monotonic()
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        with self._guards_lock:
            guard = self._guards.get(sender_key)
            if guard is None:
                if len(self._guards) >= MAX_GUARDED_SENDERS:
                    oldest = min(self._guards, key=lambda key: self._guards[key]["touched"])
                    self._guards.pop(oldest, None)
                guard = {
                    "tokens": GUARD_CAPACITY,
                    "refilled": now,
                    "touched": now,
                    "ids": {},
                    "last_body": None,
                    "last_body_at": 0.0,
                }
                self._guards[sender_key] = guard
            guard["tokens"] = min(
                GUARD_CAPACITY,
                float(guard["tokens"]) + max(0.0, now - float(guard["refilled"])) * GUARD_REFILL_PER_SECOND,
            )
            guard["refilled"] = now
            guard["touched"] = now
            cutoff = now - DEDUP_SECONDS
            guard["ids"] = {key: at for key, at in guard["ids"].items() if at >= cutoff}
            if message_id in guard["ids"]:
                return "duplicate"
            if digest == guard["last_body"] and now - float(guard["last_body_at"]) < DEDUP_SECONDS:
                return "duplicate"
            if float(guard["tokens"]) < 1.0:
                return "rate-limited"
            guard["tokens"] = float(guard["tokens"]) - 1.0
            guard["ids"][message_id] = now
            guard["last_body"] = digest
            guard["last_body_at"] = now
        return ""

    # -- receipts ----------------------------------------------------------

    def _fail(self, sender: PeerSession | None, message_id: str, reason: str, detail: str) -> None:
        self._on_error(f"dropped {message_id[:8]} ({reason}): {detail}")
        if sender is not None and self._outbox is not None:
            try:
                self._outbox.report_drop(
                    sender, original_message_id=message_id, drop_reason=reason, reason=detail
                )
            except Exception:
                pass

    def _refuse(self, sender: PeerSession | None, message_id: str, detail: str) -> None:
        self._on_error(f"refused {message_id[:8]}: {detail}")
        if sender is not None and self._outbox is not None:
            try:
                self._outbox.report_refusal(
                    sender, original_message_id=message_id, reason=detail
                )
            except Exception:
                pass

    _outbox = None

    def attach_outbox(self, outbox) -> None:
        """Give the inbox a way to send failure receipts back to a sender."""
        self._outbox = outbox
