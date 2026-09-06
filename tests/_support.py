"""Helpers shared by more than one test module."""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any

from ping_lucas.identity import pid_domain, proc_start
from ping_lucas.registry import key_filename

SENDER_SESSION = "22222222-2222-4222-8222-222222222222"


def write_private_json(path: Path, value: dict[str, Any]) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def peer_record(
    pid: int,
    socket_path: Path | str,
    *,
    session_id: str = SENDER_SESSION,
    name: str = "claude-echo",
    **overrides: Any,
) -> dict[str, Any]:
    """A record shaped exactly like the one a live Claude session writes."""
    record: dict[str, Any] = {
        "cwd": "/work/peer",
        "entrypoint": "cli",
        "kind": "interactive",
        "messagingSocketPath": str(socket_path),
        "name": name,
        "peerFeatures": [],
        "peerProtocol": 1,
        "pid": pid,
        "procStart": proc_start(pid),
        "sessionId": session_id,
        "status": "busy",
        "version": "2.1.0",
    }
    domain = pid_domain(pid)
    if domain:
        record["pidDomain"] = domain
    record.update(overrides)
    return record


def publish_peer(
    sessions_dir: Path,
    pid: int,
    socket_path: Path | str,
    token: str,
    **kwargs: Any,
) -> tuple[Path, Path]:
    """Write ``<pid>.json`` plus its key, the way Claude publishes a session."""
    record = peer_record(pid, socket_path, **kwargs)
    key: dict[str, Any] = {"peerToken": token, "procStart": record["procStart"]}
    if "pidDomain" in record:
        key["pidDomain"] = record["pidDomain"]
    key_path = write_private_json(
        sessions_dir / key_filename(pid, str(socket_path)), key
    )
    record_path = write_private_json(sessions_dir / f"{pid}.json", record)
    return record_path, key_path


def bound_socket(path: Path) -> socket.socket:
    """A listening AF_UNIX socket at ``path``, mode 0600."""
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    path.chmod(0o600)
    listener.listen(4)
    return listener
