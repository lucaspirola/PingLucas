"""Filesystem primitives that refuse to trust a directory they did not vet.

PingLucas writes an authentication token into a shared-ish directory
(``~/.claude/sessions``) and reads other sessions' tokens back out of it.  Every
one of those operations goes through this module, which enforces the same rule
each time: the file must be a regular file we own, small, not group- or
world-accessible, and reached without traversing a symlink or a directory
somebody else can write to.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .errors import RegistryError

MAX_RECORD_BYTES = 256 * 1024
MAX_KEY_BYTES = 4096


def is_private_dir(path: Path) -> bool:
    """True when ``path`` is a real directory we own that others cannot write."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and info.st_uid == os.getuid()
        and not (info.st_mode & 0o022)
    )


def ancestors_are_trusted(path: Path) -> bool:
    """Walk to ``/`` rejecting symlinked, foreign-owned or writable parents.

    A sticky directory such as ``/tmp`` is accepted: the sticky bit means only
    the owner can rename or unlink our leaf, which is the property we need.
    """
    current = path
    while current != current.parent:
        try:
            info = current.lstat()
        except OSError:
            return False
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            return False
        if info.st_uid not in (0, os.getuid()):
            return False
        if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
            return False
        current = current.parent
    return True


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` mode 0700 if absent, then assert it is safe to use."""
    try:
        path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=False)
        except FileExistsError:
            pass
    info = path.lstat()
    if (
        stat.S_ISLNK(info.st_mode)
        or not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise RegistryError(f"directory must be owned by this user with mode 0700: {path}")
    if not ancestors_are_trusted(path):
        raise RegistryError(f"directory has an untrusted ancestor: {path}")
    return path


def read_json(path: Path, limit: int, *, private: bool = False) -> dict[str, Any] | None:
    """Read a bounded JSON object, or return ``None`` if anything looks wrong.

    ``private=True`` additionally requires mode 0600 -- used for peer key files,
    whose contents are bearer tokens.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            return None
        if info.st_size > limit or info.st_mode & 0o022:
            return None
        if private and info.st_mode & 0o077:
            return None
        raw = os.read(fd, limit + 1)
    finally:
        os.close(fd)
    if len(raw) > limit:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def write_json(path: Path, value: dict[str, Any], mode: int = 0o600) -> None:
    """Atomically replace ``path`` with ``value``.

    Readers see either the old record or the new one, never a truncated file.
    """
    ensure_private_dir(path.parent)
    fd, temporary = tempfile.mkstemp(prefix=".ping-lucas-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        payload = (json.dumps(value, separators=(",", ":"), sort_keys=True) + "\n").encode()
        with os.fdopen(fd, "wb", closefd=True) as stream:
            fd = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
