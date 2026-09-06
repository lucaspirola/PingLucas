"""Linux process-identity helpers.

Every peer record PingLucas writes is bound to one *process generation*: a
(pid, start-tick, pid-namespace) triple.  A record that outlives its process --
after a crash, or once the kernel recycles the pid -- therefore fails
validation instead of pointing a Claude session at somebody else's socket.
"""

from __future__ import annotations

import os
from pathlib import Path

_MACHINE_ID_PATHS = ("/etc/machine-id", "/var/lib/dbus/machine-id")


def _stat_field(pid: int, index: int) -> str:
    """Read one field of ``/proc/<pid>/stat`` after the comm parenthesis.

    Splitting on the last ``)`` is deliberate: a process may name itself
    ``evil) 0 0 0`` and a naive ``split()`` would hand back attacker text.
    """
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return data.rsplit(")", 1)[1].split()[index]
    except (OSError, IndexError, ValueError):
        return ""


def proc_start(pid: int) -> str:
    """Return the process start time in clock ticks since boot (stat field 22)."""
    return _stat_field(pid, 19)


def proc_state(pid: int) -> str:
    return _stat_field(pid, 0)


def proc_parent(pid: int) -> int | None:
    value = _stat_field(pid, 1)
    try:
        parent = int(value)
    except ValueError:
        return None
    return parent if parent > 0 else None


def proc_uid(pid: int) -> int | None:
    try:
        return Path(f"/proc/{pid}").stat().st_uid
    except OSError:
        return None


def proc_name(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip().lower()
    except OSError:
        return ""


def proc_namespace(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/ns/pid")
    except OSError:
        return ""


def machine_id() -> str:
    for candidate in _MACHINE_ID_PATHS:
        try:
            value = Path(candidate).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value and len(value) <= 128:
            return value
    return ""


def pid_domain(pid: int) -> str:
    """Claude's spelling for "this pid means this process on this host"."""
    namespace = proc_namespace(pid)
    machine = machine_id()
    if not namespace or not machine:
        return ""
    return f"linux:{machine}:{namespace}"


def process_alive(pid: int, start: str, namespace: str = "") -> bool:
    """True when ``pid`` is still the same live process generation."""
    if pid <= 1 or proc_uid(pid) != os.getuid() or not start:
        return False
    if proc_start(pid) != str(start):
        return False
    if proc_state(pid) in {"Z", "X"}:
        return False
    return not namespace or proc_namespace(pid) == namespace


def ancestors(pid: int | None = None, limit: int = 64) -> list[int]:
    current = pid or os.getpid()
    found: list[int] = []
    seen: set[int] = set()
    for _ in range(limit):
        if current <= 1 or current in seen:
            break
        seen.add(current)
        found.append(current)
        parent = proc_parent(current)
        if parent is None:
            break
        current = parent
    return found


def node_path_resolve(value: str) -> str:
    """Reproduce Node's ``path.resolve`` for an absolute POSIX path.

    Claude derives a peer key filename from ``sha256(path.resolve(socket))``.
    That is *lexical* normalisation -- it never touches the filesystem -- so
    ``os.path.realpath`` would produce a different digest whenever any parent
    directory happens to be a symlink.
    """
    resolved = os.path.abspath(os.path.normpath(value))
    if resolved.startswith("//"):
        resolved = "/" + resolved.lstrip("/")
    return resolved
