"""Hermetic test environment for PingLucas.

Every fixture here exists to keep the suite off the real machine: the real
``~/.claude/sessions`` is never read or written, no relay is left resident, and
no socket survives a test.  Three isolation levers do the work:

* ``HOME`` is redirected, so ``config_roots()`` cannot fall back to the real
  ``~/.claude``;
* ``registry.MAX_PROC_SCAN`` is set to 0, so the ``/proc`` sweep that harvests
  ``CLAUDE_CONFIG_DIR`` from live Claude processes finds nothing (this machine
  is very likely running one);
* ``PING_LUCAS_CLAUDE_CONFIG_DIRS`` points at a throwaway registry root.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_SRC = TESTS_DIR.parent / "plugins" / "ping-lucas" / "src"

for entry in (str(PLUGIN_SRC), str(TESTS_DIR)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from ping_lucas import registry as registry_module  # noqa: E402

_ENV_KEYS = (
    "PING_LUCAS_CLAUDE_CONFIG_DIRS",
    "PING_LUCAS_CONFIG",
    "PING_LUCAS_NAME",
    "PING_LUCAS_SESSION_ID",
    "PING_LUCAS_TRANSPORT",
    "PING_LUCAS_TELEGRAM_TOKEN",
    "PING_LUCAS_TELEGRAM_CHAT_ID",
    "PING_LUCAS_NTFY_TOPIC",
    "PING_LUCAS_NTFY_REPLY_TOPIC",
    "PING_LUCAS_NTFY_SERVER",
    "PING_LUCAS_NTFY_TOKEN",
    "PING_LUCAS_WEBHOOK_URL",
    "CLAUDE_CONFIG_DIR",
)


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Cut every path back to ``tmp_path`` before a single test body runs."""
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    (tmp_path / "state").mkdir(mode=0o700)
    (tmp_path / "config").mkdir(mode=0o700)

    # Never harvest CLAUDE_CONFIG_DIR out of the live Claude processes running
    # on the developer's machine; that would drag the real registry in.
    monkeypatch.setattr(registry_module, "MAX_PROC_SCAN", 0)

    assert Path.home() == home
    yield


@pytest.fixture
def registry_root(tmp_path, monkeypatch):
    """A fake Claude config root whose ``sessions/`` directory we may publish to."""
    root = tmp_path / "claude"
    sessions = root / "sessions"
    sessions.mkdir(parents=True, mode=0o700)
    root.chmod(0o700)
    monkeypatch.setenv("PING_LUCAS_CLAUDE_CONFIG_DIRS", str(root))
    return root


@pytest.fixture
def sessions_dir(registry_root):
    return registry_root / "sessions"


@pytest.fixture
def socket_dir():
    """A *short* 0700 directory for AF_UNIX sockets.

    ``sun_path`` is 108 bytes; pytest's ``tmp_path`` is already long enough that
    a nested socket name can overflow it, so sockets live directly under /tmp.
    """
    path = Path(tempfile.mkdtemp(prefix="plt-", dir="/tmp"))
    path.chmod(0o700)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def runtime_dir(socket_dir, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(socket_dir))
    return socket_dir


def mode_of(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def uid() -> int:
    return os.getuid()
