"""Outbound half of the peer protocol: hand a frame to a Claude session.

Sending is deliberately paranoid.  Between the moment a peer record was read
and the moment we write Lucas's words into a socket, the session may have
exited and the kernel may have handed its pid to something else.  Each send
therefore re-validates the record, checks ``SO_PEERCRED`` against it, and
re-checks the process start tick before the token leaves the process.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import uuid
from typing import Any

from .envelope import build_envelope, status_frame, user_frame
from .errors import DeliveryError, RegistryError
from .identity import proc_start
from .registry import PeerDirectory, PeerSession

CONNECT_TIMEOUT_S = 4.0
VALID_DROP_REASONS = frozenset(
    ("rate-limited", "duplicate", "hop-loop", "hop-runaway", "queue-full")
)


def peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    if not hasattr(socket, "SO_PEERCRED"):
        raise DeliveryError("peer verification requires Linux SO_PEERCRED")
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    return struct.unpack("3i", raw)


class Outbox:
    """Writes authenticated peer frames to live Claude sessions."""

    def __init__(self, directory: PeerDirectory, *, self_address: str, self_name: str):
        self.directory = directory
        self.self_address = self_address
        self.self_name = self_name

    def _connect(self, peer: PeerSession) -> tuple[PeerSession, socket.socket]:
        fresh = self.directory.refresh(peer)
        if fresh is None:
            raise DeliveryError(f"{peer.label} ended before the message could be sent")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(CONNECT_TIMEOUT_S)
        try:
            connection.connect(fresh.socket_path)
            peer_pid, peer_uid, _ = peer_credentials(connection)
            if peer_uid != os.getuid() or peer_pid != fresh.pid:
                raise DeliveryError(f"{fresh.label}'s socket does not match its registry record")
            if proc_start(peer_pid) != fresh.proc_start:
                raise DeliveryError(f"{fresh.label} restarted mid-send")
            try:
                token = self.directory.peer_token(fresh)
            except RegistryError as exc:
                raise DeliveryError(str(exc)) from exc
            _write_line(connection, {"type": "auth", "token": token})
            return fresh, connection
        except Exception:
            connection.close()
            raise

    def send_message(
        self,
        peer: PeerSession,
        message: str,
        *,
        priority: str = "now",
        message_id: str = "",
        hop_chain: str = "",
        mode_attestation: str = "bypass",
        provenance: str | None = None,
    ) -> dict[str, Any]:
        """Deliver one human message to ``peer`` and return a receipt."""
        message_id = message_id or str(uuid.uuid4())
        connection: socket.socket | None = None
        try:
            peer, connection = self._connect(peer)
            kwargs: dict[str, Any] = {}
            if provenance is not None:
                kwargs["provenance"] = provenance
            content = build_envelope(
                message,
                from_address=self.self_address,
                from_name=self.self_name,
                hop_chain=hop_chain,
                mode_attestation=mode_attestation,
                **kwargs,
            )
            _write_line(
                connection,
                user_frame(
                    message_id=message_id,
                    session_id=peer.session_id,
                    content=content,
                    from_address=self.self_address,
                    priority=priority,
                    mode_attestation=mode_attestation,
                ),
            )
        except DeliveryError:
            raise
        except (OSError, ValueError) as exc:
            raise DeliveryError(f"send to {peer.label} failed: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()
        return {
            "message_id": message_id,
            "target": peer.label,
            "target_session": peer.session_id,
            "transport": "claude-peer-v1",
            "status": "queued",
        }

    def report_drop(
        self, peer: PeerSession, *, original_message_id: str, drop_reason: str, reason: str
    ) -> None:
        """Tell a sender we deliberately did not enqueue their message."""
        if not original_message_id:
            return
        if drop_reason not in VALID_DROP_REASONS:
            drop_reason = "queue-full"
        self._send_status(
            peer,
            status="dropped",
            original_message_id=original_message_id,
            reason=reason,
            drop_reason=drop_reason,
        )

    def report_refusal(self, peer: PeerSession, *, original_message_id: str, reason: str) -> None:
        """Claude's compatibility spelling for a refused peer message."""
        if not original_message_id:
            return
        self._send_status(
            peer,
            status="expired",
            original_message_id=original_message_id,
            reason=reason,
            extra={"status_detail": "refused"},
        )

    def _send_status(
        self,
        peer: PeerSession,
        *,
        status: str,
        original_message_id: str,
        reason: str,
        drop_reason: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        connection: socket.socket | None = None
        try:
            peer, connection = self._connect(peer)
            frame = status_frame(
                message_id=str(uuid.uuid4()),
                original_message_id=original_message_id,
                from_address=self.self_address,
                status=status,
                reason=reason,
                drop_reason=drop_reason,
            )
            if extra:
                frame.update(extra)
            _write_line(connection, frame)
        except DeliveryError:
            raise
        except (OSError, ValueError) as exc:
            raise DeliveryError(f"status send to {peer.label} failed: {exc}") from exc
        finally:
            if connection is not None:
                connection.close()


def _write_line(connection: socket.socket, value: dict[str, Any]) -> None:
    connection.sendall((json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8"))
