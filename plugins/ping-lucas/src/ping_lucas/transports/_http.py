"""A tiny stdlib HTTP helper.

PingLucas deliberately has no third-party dependencies: it runs as a resident
daemon on a developer's machine, and a supply chain is one more thing that can
break at 3am. ``urllib`` is enough for every transport here.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..errors import TransportError

USER_AGENT = "PingLucas/0.1 (+https://github.com/lucaspirola/PingLucas)"


def request(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> bytes:
    final_headers = {"User-Agent": USER_AGENT}
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        final_headers["Content-Type"] = "application/json"
    final_headers.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=final_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:512].decode("utf-8", "replace")
        raise TransportError(f"{method} {_redact(url)} -> HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise TransportError(f"{method} {_redact(url)} failed: {exc}") from exc


def request_json(url: str, **kwargs: Any) -> Any:
    raw = request(url, **kwargs)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise TransportError(f"{_redact(url)} did not return JSON") from exc


def _redact(url: str) -> str:
    """Keep bot tokens out of logs and error messages."""
    parts = url.split("/")
    return "/".join(part if len(part) < 24 or ":" not in part else "<token>" for part in parts)
