"""Who asked what, so a wrist-typed reply reaches the right agent.

Lucas answers on a watch: a few words, no addressing, often minutes later and
sometimes while three agents are waiting.  The ledger is what makes that
usable.  Every inbound ping gets a four-character tag; the reply is routed by,
in order of confidence:

1. an explicit ``a3f2 yes`` prefix,
2. the transport's own reply linkage (Telegram's ``reply_to_message_id``),
3. the most recent question that is still unanswered.

Entries are persisted so a relay restart does not orphan a pending question,
and they carry the sender's full process generation so a stale tag can never
be resolved against a recycled pid.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .registry import PeerSession
from .safeio import ensure_private_dir, read_json, write_json

# Base32 without the characters that are ambiguous when read off a small
# screen or dictated aloud: no 0/o, 1/l/i, u (sounds like "you").
TAG_ALPHABET = "abcdefghjkmnpqrstvwxyz23456789"
TAG_LENGTH = 4
TAG_RE = re.compile(rf"^[#\[]?([{TAG_ALPHABET}]{{{TAG_LENGTH}}})[\]:.,]?\s+(.*)$", re.DOTALL)
BARE_TAG_RE = re.compile(rf"^[#\[]?([{TAG_ALPHABET}]{{{TAG_LENGTH}}})[\]:.,]?$")

MAX_ENTRIES = 200
MAX_LEDGER_BYTES = 1024 * 1024
DEFAULT_TTL_S = 24 * 60 * 60


@dataclass
class Entry:
    """One outstanding question from an agent."""

    tag: str
    message_id: str
    session_id: str
    pid: int
    proc_start: str
    socket_path: str
    registry_dir: str
    record_path: str
    label: str
    cwd: str = ""
    preview: str = ""
    hop_chain: str = ""
    priority: str = "now"
    created_at: float = field(default_factory=time.time)
    answered_at: float = 0.0
    external_ref: str = ""

    @property
    def answered(self) -> bool:
        return self.answered_at > 0.0

    def to_peer(self) -> PeerSession:
        return PeerSession(
            session_id=self.session_id,
            name=self.label,
            pid=self.pid,
            proc_start=self.proc_start,
            socket_path=self.socket_path,
            registry_dir=self.registry_dir,
            record_path=self.record_path,
            cwd=self.cwd,
        )


class Ledger:
    """A small, bounded, persisted map from reply tag to waiting agent."""

    def __init__(self, path: Path, *, ttl_s: float = DEFAULT_TTL_S, max_entries: int = MAX_ENTRIES):
        self.path = Path(path)
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: dict[str, Entry] = {}
        self._load()

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        ensure_private_dir(self.path.parent)
        raw = read_json(self.path, MAX_LEDGER_BYTES, private=True)
        if not raw:
            return
        for item in raw.get("entries", [])[: self.max_entries]:
            if not isinstance(item, dict):
                continue
            try:
                entry = Entry(**{key: item[key] for key in item if key in Entry.__annotations__})
            except (TypeError, ValueError):
                continue
            self._entries[entry.tag] = entry
        self._evict()

    def _save(self) -> None:
        write_json(
            self.path,
            {"version": 1, "entries": [asdict(entry) for entry in self._entries.values()]},
            0o600,
        )

    def _evict(self) -> None:
        cutoff = time.time() - self.ttl_s
        for tag, entry in list(self._entries.items()):
            if entry.created_at < cutoff:
                del self._entries[tag]
        if len(self._entries) > self.max_entries:
            ordered = sorted(self._entries.values(), key=lambda e: e.created_at)
            for entry in ordered[: len(self._entries) - self.max_entries]:
                self._entries.pop(entry.tag, None)

    # -- writing -----------------------------------------------------------

    def _new_tag(self) -> str:
        for _ in range(64):
            tag = "".join(secrets.choice(TAG_ALPHABET) for _ in range(TAG_LENGTH))
            if tag not in self._entries:
                return tag
        # Astronomically unlikely; recycle the oldest slot rather than fail.
        oldest = min(self._entries.values(), key=lambda e: e.created_at)
        del self._entries[oldest.tag]
        return oldest.tag

    def record(self, peer: PeerSession, message_id: str, body: str, *, hop_chain: str = "", priority: str = "now") -> Entry:
        with self._lock:
            entry = Entry(
                tag=self._new_tag(),
                message_id=message_id,
                session_id=peer.session_id,
                pid=peer.pid,
                proc_start=peer.proc_start,
                socket_path=peer.socket_path,
                registry_dir=peer.registry_dir,
                record_path=peer.record_path,
                label=peer.label,
                cwd=peer.cwd,
                preview=" ".join(body.split())[:200],
                hop_chain=hop_chain,
                priority=priority,
            )
            self._entries[entry.tag] = entry
            # Evict *after* inserting: evicting first leaves the ledger one
            # over its cap until the next read happens to trim it.
            self._evict()
            self._save()
            return entry

    def link_external(self, tag: str, external_ref: str) -> None:
        """Associate a transport-side id (e.g. a Telegram message id) with a tag."""
        with self._lock:
            entry = self._entries.get(tag)
            if entry is not None and external_ref:
                entry.external_ref = str(external_ref)
                self._save()

    def mark_answered(self, tag: str) -> None:
        with self._lock:
            entry = self._entries.get(tag)
            if entry is not None:
                entry.answered_at = time.time()
                self._save()

    # -- reading -----------------------------------------------------------

    def get(self, tag: str) -> Entry | None:
        with self._lock:
            return self._entries.get(tag)

    def by_external(self, external_ref: str) -> Entry | None:
        if not external_ref:
            return None
        with self._lock:
            for entry in self._entries.values():
                if entry.external_ref and entry.external_ref == str(external_ref):
                    return entry
        return None

    def pending(self) -> list[Entry]:
        with self._lock:
            self._evict()
            return sorted(
                (e for e in self._entries.values() if not e.answered),
                key=lambda e: e.created_at,
                reverse=True,
            )

    def recent(self, limit: int = 20) -> list[Entry]:
        with self._lock:
            self._evict()
            return sorted(self._entries.values(), key=lambda e: e.created_at, reverse=True)[:limit]

    def latest(self) -> Entry | None:
        candidates = self.pending()
        if candidates:
            return candidates[0]
        recent = self.recent(1)
        return recent[0] if recent else None


def split_tag(text: str) -> tuple[str, str]:
    """Split ``"a3f2 ship it"`` into ``("a3f2", "ship it")``.

    A bare tag with no body is treated as a tag with an empty message so the
    caller can decide whether that means "acknowledge" or "nothing to send".
    """
    stripped = text.strip()
    bare = BARE_TAG_RE.match(stripped)
    if bare:
        return bare.group(1), ""
    match = TAG_RE.match(stripped)
    if match:
        return match.group(1), match.group(2).strip()
    return "", stripped
