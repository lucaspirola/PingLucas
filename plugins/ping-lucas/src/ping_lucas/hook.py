#!/usr/bin/env python3
"""SessionStart hook: tell each new session that the human is addressable.

Agents will not message a peer they do not know exists.  This hook injects one
short line naming the live relay, so every session -- and every subagent that
inherits the context -- knows the address and when to use it.

It is deliberately fail-open and fast: a relay that is down, a config that was
never written, or any unexpected error must never delay or block a session.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MAX_STDIN_BYTES = 128 * 1024


def relay_status() -> dict | None:
    """Return the running relay's pid record, or ``None`` if it is not up."""
    from ping_lucas.config import Config, state_home
    from ping_lucas.identity import process_alive
    from ping_lucas.safeio import read_json

    record = read_json(state_home() / "relay.pid", 4096)
    if not record:
        return None
    pid = record.get("pid")
    if not isinstance(pid, int) or not process_alive(pid, str(record.get("procStart", ""))):
        return None
    try:
        record["config"] = Config.load()
    except Exception:
        record["config"] = None
    return record


def context_line(record: dict) -> str:
    name = record.get("name", "lucas")
    config = record.get("config")
    transport = ""
    if config is not None:
        transport = ", ".join(config.transports)
    where = f" (reaches him via {transport})" if transport else ""
    return (
        f"PingLucas is live: the human operator is on this machine's session roster as "
        f"`{name}`{where}. `ListAgents` will show him as a peer session; `SendMessage` to "
        f"`{name}` puts one line on his Apple Watch and his reply comes straight back to "
        f"this session.\n\n"
        f"Use it when you are genuinely blocked on a decision only he can make -- an "
        f"irreversible or outward-facing action, an ambiguous requirement, a missing "
        f"credential -- or to report that long-running work finished. Ask one specific, "
        f"self-contained question in the first line: he is reading it on a watch, so a "
        f"yes/no or a short choice gets answered in seconds and a paragraph gets ignored. "
        f"Do not ping him for anything you can determine yourself, and do not ping him "
        f"repeatedly about the same thing."
    )


def main() -> int:
    try:
        sys.stdin.read(MAX_STDIN_BYTES)  # drain the hook payload; we need nothing from it
    except Exception:
        pass
    try:
        record = relay_status()
        if record is None:
            return 0
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": context_line(record),
                    }
                }
            )
        )
    except Exception as exc:
        # Never let awareness plumbing get in the way of the session starting.
        print(f"ping-lucas hook skipped: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
