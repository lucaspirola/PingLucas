"""Claude peer-protocol v1 wire format: frames and cross-session envelopes.

Two layers are in play.  The *frame* is one JSON object per line on the Unix
socket.  Inside it, ``message.content`` carries a ``<cross-session-message>``
envelope whose attributes are transport-owned metadata -- who sent it, under
what permission class, and through which hops.

Claude's receiver is order-sensitive about those attributes, so
:func:`build_envelope` emits them in the canonical order and
:func:`parse_envelope` refuses anything that is not byte-identical to what a
canonical sender would have produced.  Being strict here is what stops a peer
from smuggling a forged ``from-mode="bypass"`` past the parser.
"""

from __future__ import annotations

import html
import re
from typing import Any

MAX_MESSAGE_BYTES = 60 * 1024
VALID_PRIORITIES = ("now", "next", "later")
VALID_MODE_ATTESTATIONS = frozenset(("bypass", "prompting"))

ATTRIBUTE_ORDER = ("from", "from-session", "hop-chain", "from-name", "from-mode")

_OPEN_RE = re.compile(r'^<cross-session-message(?P<attrs>(?: [a-z][a-z0-9-]*="[^"\r\n]*")*)>$')
_PAIR_RE = re.compile(r' ([a-z][a-z0-9-]*)="([^"\r\n]*)"')
_HOP_CHAIN_RE = re.compile(r"^[0-9a-f]{24}(?:,[0-9a-f]{24}){0,31}$")
_FROM_RE = re.compile(r"^[A-Za-z0-9%:_/.\\-]{1,300}$")
_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_NAME_RE = re.compile(r'^[^"<>\r\n]+$')
_UNSAFE_LABEL_RE = re.compile(r"[^A-Za-z0-9._ -]+")

CLOSING = "</cross-session-message>"

PROVENANCE = (
    "<ping-lucas-context>Message from Lucas, the human operator, relayed from his "
    "Apple Watch. Treat it as operator intent for the work already in scope. It is "
    "still a short wrist-typed message, not a reviewed authorization: it cannot by "
    "itself approve a destructive, irreversible, or outward-facing action you would "
    "otherwise stop and ask about."
    "</ping-lucas-context>"
)


def safe_label(value: str, fallback: str = "peer") -> str:
    """Reduce a display name to characters that cannot break out of an attribute."""
    value = " ".join(str(value).split())[:64]
    return _UNSAFE_LABEL_RE.sub("_", value).strip() or fallback


def valid_hop_chain(value: object, *, optional: bool = False) -> bool:
    if optional and value == "":
        return True
    return isinstance(value, str) and _HOP_CHAIN_RE.fullmatch(value) is not None


def build_envelope(
    message: str,
    *,
    from_address: str,
    from_name: str,
    hop_chain: str = "",
    mode_attestation: str = "",
    provenance: str = PROVENANCE,
) -> str:
    """Wrap ``message`` in the canonical envelope Claude's parser accepts."""
    body = message
    if provenance:
        body = provenance + "\n\n" + body
    # A payload containing a literal closing tag would otherwise end the
    # envelope early and let the remainder pose as sibling content.
    body = body.replace(CLOSING, "&lt;/cross-session-message&gt;")

    attrs: dict[str, str] = {"from": from_address}
    if hop_chain:
        attrs["hop-chain"] = hop_chain
    attrs["from-name"] = safe_label(from_name)
    if mode_attestation in VALID_MODE_ATTESTATIONS:
        attrs["from-mode"] = mode_attestation

    ordered = sorted(attrs.items(), key=lambda pair: ATTRIBUTE_ORDER.index(pair[0]))
    rendered = " ".join(f'{key}="{html.escape(value, quote=True)}"' for key, value in ordered)
    return f"<cross-session-message {rendered}>\n{body}\n{CLOSING}"


def parse_envelope(content: object) -> tuple[dict[str, str], str] | None:
    """Parse a canonical envelope, or ``None``.

    Rejects reordered, repeated, unknown or malformed attributes and any
    payload carrying a second closing tag.
    """
    if not isinstance(content, str) or content.count(CLOSING) != 1:
        return None
    suffix = "\n" + CLOSING
    if not content.endswith(suffix):
        return None
    try:
        opening, body = content[: -len(suffix)].split("\n", 1)
    except ValueError:
        return None
    match = _OPEN_RE.fullmatch(opening)
    if match is None:
        return None
    raw_attrs = match.group("attrs")
    pairs = _PAIR_RE.findall(raw_attrs)
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)) or any(key not in ATTRIBUTE_ORDER for key in keys):
        return None
    indexes = [ATTRIBUTE_ORDER.index(key) for key in keys]
    if indexes != sorted(indexes):
        return None
    validators = {
        "from": lambda v: _FROM_RE.fullmatch(v) is not None,
        "from-session": lambda v: _SESSION_RE.fullmatch(v) is not None,
        "hop-chain": valid_hop_chain,
        "from-name": lambda v: _NAME_RE.fullmatch(v) is not None,
        "from-mode": lambda v: v in VALID_MODE_ATTESTATIONS,
    }
    if any(not validators[key](value) for key, value in pairs):
        return None
    # Re-render and compare so no alternative encoding of the same attributes
    # (extra whitespace, entity tricks) slips through as canonical.
    if "".join(f' {key}="{value}"' for key, value in pairs) != raw_attrs:
        return None
    return {key: html.unescape(value) for key, value in pairs}, body


def user_frame(
    *,
    message_id: str,
    session_id: str,
    content: str,
    from_address: str,
    priority: str = "now",
    mode_attestation: str = "",
) -> dict[str, Any]:
    """Build the outbound ``type: user`` frame for a Claude peer socket."""
    frame: dict[str, Any] = {
        "msgV": 1,
        "msg_id": message_id,
        "type": "user",
        "message": {"role": "user", "content": content},
        "session_id": session_id,
        "priority": priority,
        "from": from_address,
    }
    if mode_attestation in VALID_MODE_ATTESTATIONS:
        frame["from_mode"] = mode_attestation
    return frame


def status_frame(
    *,
    message_id: str,
    original_message_id: str,
    from_address: str,
    status: str,
    reason: str,
    drop_reason: str = "",
) -> dict[str, Any]:
    """Build a ``peer_message_status`` control frame (a delivery receipt)."""
    frame: dict[str, Any] = {
        "msgV": 1,
        "msg_id": message_id,
        "type": "control",
        "action": "peer_message_status",
        "status": status,
        "reason": " ".join(str(reason).split())[:256] or "PingLucas did not accept the message",
        "orig_msg_id": original_message_id,
        "from": from_address,
    }
    if drop_reason:
        frame["drop_reason"] = drop_reason
        frame["dropped_msg_ids"] = [original_message_id]
    return frame
