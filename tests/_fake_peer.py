"""A fake Claude peer that lives in its own process.

``SO_PEERCRED`` is the load-bearing check on both halves of the peer protocol:
the inbox binds a claimed ``uds:`` address to the pid that actually connected,
and the outbox refuses to write a token into a socket whose peer pid disagrees
with the registry record.  Neither can be exercised from a thread, so the fake
peer must be a real, separately-scheduled process with a pid of its own.

The parent drives it over a :class:`multiprocessing.Pipe`; the child owns an
AF_UNIX listener so status frames the relay sends *back* can be captured.
"""

from __future__ import annotations

import json
import os
import socket
import struct
from pathlib import Path
from typing import Any

READY_TIMEOUT_S = 10.0


def _read_frames(connection: socket.socket, limit: int = 8) -> tuple[int, list[dict]]:
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    pid, _, _ = struct.unpack("3i", raw)
    frames: list[dict] = []
    connection.settimeout(2.0)
    try:
        with connection.makefile("rb") as stream:
            for _ in range(limit):
                line = stream.readline(1 << 20)
                if not line:
                    break
                try:
                    frames.append(json.loads(line.decode("utf-8")))
                except ValueError:
                    break
    except (OSError, socket.timeout):
        pass
    return pid, frames


def peer_main(connection, socket_dir: str, token: str) -> None:  # pragma: no cover - child
    os.umask(0o077)
    pid = os.getpid()
    socket_path = Path(socket_dir) / f"{pid}.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(socket_path))
        os.chmod(socket_path, 0o600)
        listener.listen(16)
        connection.send({"pid": pid, "socket_path": str(socket_path), "token": token})
        while True:
            command = connection.recv()
            operation = command.get("op")
            if operation == "stop":
                return
            sent = 0
            if operation == "send":
                sent = _send(command)
            received: list[dict] = []
            callers: list[int] = []
            deadline = float(command.get("collect_s", 0.0))
            listener.settimeout(deadline if deadline > 0 else 0.01)
            while deadline > 0:
                try:
                    incoming, _ = listener.accept()
                except (socket.timeout, TimeoutError):
                    break
                except OSError:
                    break
                try:
                    caller, frames = _read_frames(incoming)
                finally:
                    incoming.close()
                callers.append(caller)
                received.extend(frames)
                if len(received) >= int(command.get("expect", 1)):
                    # Give a moment for a second, unexpected receipt to show up.
                    listener.settimeout(0.15)
            connection.send({"sent": sent, "frames": received, "callers": callers})
    finally:
        listener.close()
        try:
            socket_path.unlink()
        except OSError:
            pass
        try:
            connection.close()
        except OSError:
            pass


def _send(command: dict[str, Any]) -> int:
    """Write auth + frames to the relay, splitting across connections."""
    frames = list(command["frames"])
    chunk = int(command.get("chunk", 8))
    auth_token = command.get("target_token")
    sent = 0
    for start in range(0, max(1, len(frames)), chunk):
        batch = frames[start : start + chunk]
        outgoing = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        outgoing.settimeout(5.0)
        try:
            outgoing.connect(command["target_socket"])
            _write(outgoing, {"type": "auth", "token": auth_token})
            for frame in batch:
                _write(outgoing, frame)
                sent += 1
        except (BrokenPipeError, ConnectionResetError, OSError):
            # A relay that rejects the auth line closes immediately; that is a
            # legitimate outcome, not a harness failure.
            pass
        finally:
            outgoing.close()
        if not frames:
            break
    return sent


def _write(connection: socket.socket, value: dict) -> None:
    connection.sendall((json.dumps(value, separators=(",", ":")) + "\n").encode("utf-8"))
