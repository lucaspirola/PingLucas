"""Discovery of, and publication into, Claude Code's session registries.

A running Claude Code session advertises itself by writing
``<config>/sessions/<pid>.json`` and, next to it, a private
``<pid>.<sha256(socket)>.key`` holding the bearer token a peer must present to
open its Unix socket.  PingLucas does both halves of that contract:

* :class:`PeerDirectory` reads other sessions' records so we can address them.
* :class:`RecordPublisher` writes our own, which is what makes Lucas appear in
  ``ListAgents`` as an ordinary peer session.

Nothing here patches Claude, edits its settings, or installs a hook into it.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, unquote

from . import PEER_PROTOCOL, __version__
from .errors import RegistryError
from .identity import (
    machine_id,
    node_path_resolve,
    pid_domain,
    proc_name,
    proc_start,
    proc_uid,
)
from .safeio import (
    MAX_KEY_BYTES,
    MAX_RECORD_BYTES,
    ensure_private_dir,
    is_private_dir,
    read_json,
    unlink_quietly,
    write_json,
)

MAX_PROC_ENV_BYTES = 256 * 1024
MAX_PROC_SCAN = 4096
MAX_RECORDS_PER_DIR = 4096
_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32}$")


@dataclass(frozen=True)
class PeerSession:
    """One live Claude session we can hand a frame to."""

    session_id: str
    name: str
    pid: int
    proc_start: str
    socket_path: str
    registry_dir: str
    record_path: str
    cwd: str = ""
    status: str = "unknown"
    kind: str = ""
    version: str = ""
    pid_domain: str = ""
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def address(self) -> str:
        """The ``uds:`` address Claude uses to route a reply back to this peer."""
        return uds_address(self.socket_path)

    @property
    def label(self) -> str:
        return self.name or f"claude-{self.session_id[:8]}"

    @property
    def key_path(self) -> Path:
        return Path(self.registry_dir) / key_filename(self.pid, self.socket_path)


def uds_address(socket_path: str) -> str:
    """Percent-encode a socket path the way Claude spells a peer address."""
    encoded = quote(str(socket_path), safe="/:._-\\").replace("~", "%7E")
    return f"uds:{encoded}"


def parse_uds_address(address: str) -> str:
    """Return the absolute socket path in a ``uds:`` address, or ``""``."""
    if not isinstance(address, str) or not address.startswith("uds:"):
        return ""
    raw = unquote(address[4:])
    if not os.path.isabs(raw) or "\x00" in raw:
        return ""
    return node_path_resolve(raw)


def key_filename(pid: int, socket_path: str) -> str:
    """Claude derives the key filename from the *lexically* resolved socket path."""
    digest = hashlib.sha256(node_path_resolve(str(socket_path)).encode("utf-8")).hexdigest()
    return f"{pid}.{digest}.key"


# ``sun_path`` is 108 bytes including the NUL; Claude keeps a margin at 103.
MAX_SOCKET_PATH_BYTES = 103


def default_socket_dir() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    if runtime and os.path.isabs(runtime):
        return Path(runtime) / "cc-socks"
    return Path(f"/tmp/cc-socks-{os.getuid()}")


def choose_socket_path(pid: int, socket_dir: Path | None = None) -> Path:
    """Pick this process's socket path, falling back when ``sun_path`` is too short.

    An unusually deep ``XDG_RUNTIME_DIR`` can produce a path that ``bind()``
    silently truncates.  Claude handles that by dropping to its short per-user
    ``/tmp`` directory, so we do the same and stay discoverable.
    """
    directory = Path(node_path_resolve(str(socket_dir or default_socket_dir())))
    candidate = directory / f"{pid}.sock"
    if len(os.fsencode(candidate)) > MAX_SOCKET_PATH_BYTES and socket_dir is None:
        candidate = Path(f"/tmp/cc-socks-{os.getuid()}") / f"{pid}.sock"
    if len(os.fsencode(candidate)) > MAX_SOCKET_PATH_BYTES:
        raise RegistryError(f"socket path exceeds the Unix socket limit: {candidate}")
    return candidate


def _claude_config_dir_of(pid: int) -> str:
    """Read ``CLAUDE_CONFIG_DIR`` from a live Claude process's environment.

    Sessions started against an alternate profile keep their registry somewhere
    other than ``~/.claude``; without this they would be invisible to us and,
    worse, we would be invisible to them.
    """
    name = proc_name(pid)
    if proc_uid(pid) != os.getuid() or not (name == "claude" or name.startswith("claude-")):
        return ""
    try:
        fd = os.open(f"/proc/{pid}/environ", os.O_RDONLY)
        try:
            raw = os.read(fd, MAX_PROC_ENV_BYTES + 1)
        finally:
            os.close(fd)
    except OSError:
        return ""
    if len(raw) > MAX_PROC_ENV_BYTES:
        return ""
    for item in raw.split(b"\0"):
        if item.startswith(b"CLAUDE_CONFIG_DIR="):
            try:
                return item.split(b"=", 1)[1].decode("utf-8")
            except UnicodeDecodeError:
                return ""
    return ""


def config_roots(extra: Iterable[str] = ()) -> list[Path]:
    """Every Claude profile root on this machine that has a usable registry."""
    candidates: list[str] = [str(value) for value in extra]
    configured = os.environ.get("PING_LUCAS_CLAUDE_CONFIG_DIRS", "")
    if configured:
        candidates.extend(piece for piece in configured.split(os.pathsep) if piece)
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        candidates.append(os.environ["CLAUDE_CONFIG_DIR"])
    candidates.append(str(Path.home() / ".claude"))
    try:
        pids = [int(name) for name in os.listdir("/proc") if name.isdigit()][:MAX_PROC_SCAN]
    except OSError:
        pids = []
    for pid in pids:
        value = _claude_config_dir_of(pid)
        if value:
            candidates.append(value)

    roots: list[Path] = []
    seen: set[str] = set()
    for value in candidates:
        root = Path(value).expanduser()
        if not root.is_absolute():
            continue
        normalized = os.path.normpath(str(root))
        if normalized in seen:
            continue
        seen.add(normalized)
        if is_private_dir(Path(normalized) / "sessions"):
            roots.append(Path(normalized))
    return roots


def registry_dirs(extra: Iterable[str] = ()) -> list[Path]:
    return [root / "sessions" for root in config_roots(extra)]


class PeerDirectory:
    """Read-only view of the live Claude sessions on this machine.

    A full scan reads every record in every registry root, and a busy machine
    accumulates hundreds of them.  Since the inbox resolves a sender on every
    inbound frame, an uncached scan would mean thousands of file reads a
    minute; the cache is short enough that a session appearing or ending is
    still noticed promptly, and every path that actually *sends* re-validates
    its target against disk anyway.
    """

    #: Seconds a listing may be reused. Deliberately shorter than Claude's own
    #: roster refresh, so we never look staler than the tool the user sees.
    CACHE_TTL_S = 1.0

    def __init__(self, extra_roots: Iterable[str] = (), self_pid: int | None = None):
        self._extra_roots = list(extra_roots)
        self._self_pid = self_pid
        self._cache: list[PeerSession] = []
        self._cached_at = 0.0

    def load_record(self, path: Path) -> PeerSession | None:
        """Validate one ``<pid>.json`` and turn it into an addressable peer.

        Every check here exists to stop us sending Lucas's words to the wrong
        process: the filename must agree with the record, the pid must still be
        ours and still be the same process generation, and the socket must be a
        private socket in a directory nobody else can meddle with.
        """
        registry_dir = path.parent
        if not is_private_dir(registry_dir):
            return None
        record = read_json(path, MAX_RECORD_BYTES)
        if record is None or record.get("peerProtocol") != PEER_PROTOCOL:
            return None
        # Our own record, and any other PingLucas relay, is not a Claude session.
        if record.get("pingLucas"):
            return None
        try:
            filename_pid = int(path.stem)
            pid = int(record.get("pid", filename_pid))
        except (TypeError, ValueError):
            return None
        if pid != filename_pid or pid <= 1 or pid == self._self_pid:
            return None
        if proc_uid(pid) != os.getuid():
            return None
        recorded_start = str(record.get("procStart", ""))
        if not recorded_start or proc_start(pid) != recorded_start:
            return None

        session_id = str(record.get("sessionId", ""))
        if not session_id or len(session_id) > 128 or any(ch in session_id for ch in "/\\\x00"):
            return None
        socket_value = record.get("messagingSocketPath")
        if not isinstance(socket_value, str) or not os.path.isabs(socket_value):
            return None
        socket_path = node_path_resolve(socket_value)
        if not _usable_socket(Path(socket_path)):
            return None

        return PeerSession(
            session_id=session_id,
            name=str(record.get("name", ""))[:128],
            pid=pid,
            proc_start=recorded_start,
            socket_path=socket_path,
            registry_dir=str(registry_dir),
            record_path=str(path),
            cwd=str(record.get("cwd", ""))[:4096],
            status=str(record.get("status", "unknown"))[:64],
            kind=str(record.get("kind", ""))[:64],
            version=str(record.get("version", ""))[:64],
            pid_domain=str(record.get("pidDomain", ""))[:256],
            metadata={"entrypoint": str(record.get("entrypoint", ""))[:64]},
        )

    def peers(self, *, fresh: bool = False) -> list[PeerSession]:
        now = time.monotonic()
        if not fresh and self._cache and now - self._cached_at < self.CACHE_TTL_S:
            return list(self._cache)
        found: dict[str, PeerSession] = {}
        for directory in registry_dirs(self._extra_roots):
            try:
                records = sorted(directory.glob("[0-9]*.json"))[:MAX_RECORDS_PER_DIR]
            except OSError:
                continue
            for path in records:
                peer = self.load_record(path)
                if peer is not None:
                    found[peer.session_id] = peer
        self._cache = list(found.values())
        self._cached_at = time.monotonic()
        return list(self._cache)

    def by_pid(self, pid: int) -> PeerSession | None:
        for peer in self.peers():
            if peer.pid == pid:
                return peer
        return None

    def refresh(self, peer: PeerSession) -> PeerSession | None:
        """Re-validate a peer immediately before use; sessions end all the time.

        This deliberately reads the record from disk rather than consulting the
        cache: a message is about to be written to that session's socket.
        """
        fresh = self.load_record(Path(peer.record_path))
        if fresh is None or fresh.session_id != peer.session_id:
            return None
        return fresh

    def peer_token(self, peer: PeerSession) -> str:
        """Read the bearer token that peer's socket expects."""
        key = read_json(peer.key_path, MAX_KEY_BYTES, private=True)
        if key is None:
            raise RegistryError(f"peer key for {peer.label} is missing or insecure")
        token = key.get("peerToken")
        if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
            raise RegistryError(f"peer key for {peer.label} has an unsupported token shape")
        if str(key.get("procStart", "")) != peer.proc_start:
            raise RegistryError(f"peer key for {peer.label} is from another process generation")
        if peer.pid_domain and str(key.get("pidDomain", "")) != peer.pid_domain:
            raise RegistryError(f"peer key for {peer.label} is from another pid domain")
        return token


def _usable_socket(path: Path) -> bool:
    import stat as _stat

    if not is_private_dir(path.parent) or not ancestors_ok(path.parent):
        return False
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        _stat.S_ISSOCK(info.st_mode)
        and info.st_uid == os.getuid()
        and not (info.st_mode & 0o077)
    )


def ancestors_ok(path: Path) -> bool:
    from .safeio import ancestors_are_trusted

    return ancestors_are_trusted(path)


class RecordPublisher:
    """Keep Lucas's peer record present in every live Claude registry.

    Claude discovers peers by listing its own ``sessions`` directory, so a
    session started after us would never see a record we wrote only once.
    :meth:`refresh` is therefore called on a timer, and re-publishes into any
    registry root that has appeared since the last pass.
    """

    def __init__(
        self,
        *,
        session_id: str,
        name: str,
        socket_path: Path,
        token: str,
        cwd: str = "",
        status: str = "idle",
        extra_roots: Iterable[str] = (),
        transport: str = "",
    ):
        self.session_id = session_id
        self.name = name
        self.socket_path = Path(node_path_resolve(str(socket_path)))
        self.token = token
        self.cwd = cwd or str(Path.home())
        self.status = status
        self.transport = transport
        self._extra_roots = list(extra_roots)
        self.pid = os.getpid()
        self.proc_start = proc_start(self.pid)
        if not self.proc_start:
            raise RegistryError("cannot bind a peer record to this process generation")
        self.pid_domain = pid_domain(self.pid)
        self.instance_id = secrets.token_hex(16)
        self.started_ms = int(time.time() * 1000)
        self.name_since_ms = self.started_ms
        self._published: dict[str, tuple[Path, Path]] = {}

    @property
    def address(self) -> str:
        return uds_address(str(self.socket_path))

    def record(self) -> dict[str, Any]:
        """The native peer record, field for field as Claude 2.1.x writes one."""
        record: dict[str, Any] = {
            "cwd": self.cwd,
            "entrypoint": "ping-lucas",
            "kind": "interactive",
            "messagingSocketPath": str(self.socket_path),
            "name": self.name,
            "nameSince": self.name_since_ms,
            "nameSource": "user",
            "peerFeatures": [],
            "peerProtocol": PEER_PROTOCOL,
            "pid": self.pid,
            "procStart": self.proc_start,
            "sessionId": self.session_id,
            "startedAt": self.started_ms,
            "status": self.status,
            "statusUpdatedAt": int(time.time() * 1000),
            "updatedAt": int(time.time() * 1000),
            "version": __version__,
            # Our own marker, so a second relay -- or our own PeerDirectory --
            # never mistakes this human for a Claude session.
            "pingLucas": {
                "relay": True,
                "instance": self.instance_id,
                "human": self.name,
                "transport": self.transport,
                "machine": machine_id(),
            },
        }
        if self.pid_domain:
            record["pidDomain"] = self.pid_domain
        return record

    def key(self) -> dict[str, Any]:
        value: dict[str, Any] = {"peerToken": self.token, "procStart": self.proc_start}
        if self.pid_domain:
            value["pidDomain"] = self.pid_domain
        return value

    def _targets(self) -> list[Path]:
        directories: list[Path] = []
        for directory in registry_dirs(self._extra_roots):
            try:
                ensure_private_dir(directory)
            except (RegistryError, OSError):
                continue
            directories.append(directory)
        return directories

    def refresh(self) -> list[Path]:
        """Write (or rewrite) the record and key into every live registry."""
        key_name = key_filename(self.pid, str(self.socket_path))
        key_value = self.key()
        record_value = self.record()
        written: list[Path] = []
        for directory in self._targets():
            record_path = directory / f"{self.pid}.json"
            key_path = directory / key_name
            try:
                # Key first: a record is an invitation to connect, and an
                # invitation whose key has not landed yet just fails.
                write_json(key_path, key_value, 0o600)
                write_json(record_path, record_value, 0o600)
            except (OSError, RegistryError):
                continue
            self._published[os.path.normpath(str(directory))] = (record_path, key_path)
            written.append(record_path)
        return written

    def set_status(self, status: str) -> None:
        if status != self.status:
            self.status = status
            self.refresh()

    def withdraw(self) -> None:
        """Remove the artifacts *this* generation published, and only those.

        If a replacement relay has already taken over our pid slot, its record
        is sitting at the same path.  Deleting it on our way out would make the
        live relay invisible, so every unlink is gated on the instance marker
        and process generation still being ours.
        """
        for record_path, key_path in list(self._published.values()):
            record = read_json(record_path, MAX_RECORD_BYTES)
            if (
                record is not None
                and isinstance(record.get("pingLucas"), dict)
                and record["pingLucas"].get("instance") == self.instance_id
                and record.get("procStart") == self.proc_start
                and record.get("messagingSocketPath") == str(self.socket_path)
            ):
                unlink_quietly(record_path)
            key = read_json(key_path, MAX_KEY_BYTES, private=True)
            if (
                key is not None
                and key.get("peerToken") == self.token
                and key.get("procStart") == self.proc_start
            ):
                unlink_quietly(key_path)
        self._published.clear()
