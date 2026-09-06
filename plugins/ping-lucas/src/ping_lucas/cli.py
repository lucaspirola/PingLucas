"""``pinglucas`` -- set up, run and inspect the relay."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__
from .config import Config, config_path, state_home
from .daemon import Relay
from .errors import PingLucasError
from .identity import proc_name, process_alive, proc_start
from .ledger import Ledger
from .outbox import Outbox
from .registry import PeerDirectory, config_roots, registry_dirs, uds_address
from .safeio import ensure_private_dir, read_json, write_json
from .transports import IncomingReply, build, available

PID_FILE = "relay.pid"


# ---------------------------------------------------------------- utilities


def _log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def _pid_file() -> Path:
    return state_home() / PID_FILE


def _running() -> dict | None:
    """Return the live relay's pid record, or ``None``."""
    record = read_json(_pid_file(), 4096)
    if not record:
        return None
    pid = record.get("pid")
    if not isinstance(pid, int) or not process_alive(pid, str(record.get("procStart", ""))):
        return None
    return record


def _short(value: str, width: int = 60) -> str:
    value = " ".join(str(value).split())
    return value if len(value) <= width else value[: width - 1] + "…"


# ---------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> int:
    """Write a config file, generating whatever secrets the transport needs."""
    path = Path(args.config) if args.config else config_path()
    config = Config.load(path) if path.exists() else Config()
    config.name = args.name or config.name
    config.ensure_session_id()
    config.cwd = config.cwd or str(Path.home())

    transport = args.transport
    if transport == "telegram":
        token = args.telegram_token or config.settings.get("telegram", {}).get("token", "")
        if not token:
            raise PingLucasError(
                "telegram needs a bot token: create one with @BotFather, then rerun with "
                "--telegram-token <token>"
            )
        settings = {"token": token}
        chat_id = args.telegram_chat_id or config.settings.get("telegram", {}).get("chat_id", "")
        if not chat_id:
            probe = build("telegram", {"token": token})
            chat_id = probe.discover_chat_id()
            if chat_id:
                _log(f"learned chat id {chat_id} from your last message to the bot")
            else:
                _log("no chat id yet — message the bot once, then run `pinglucas doctor --learn-chat`")
        if chat_id:
            settings["chat_id"] = chat_id
        config.settings["telegram"] = settings
    elif transport == "ntfy":
        existing = config.settings.get("ntfy", {})
        topic = args.ntfy_topic or existing.get("topic") or f"pinglucas-{secrets.token_hex(8)}"
        reply = existing.get("reply_topic") or f"{topic}-reply"
        config.settings["ntfy"] = {
            "server": args.ntfy_server or existing.get("server", "https://ntfy.sh"),
            "topic": topic,
            "reply_topic": reply,
        }
        _log(f"subscribe your phone to topic:  {topic}")
        _log(f"send replies to topic:          {reply}")
    elif transport == "webhook":
        if not args.webhook_url:
            raise PingLucasError("webhook transport needs --webhook-url")
        config.settings["webhook"] = {"url": args.webhook_url}

    config.transports = [transport]
    config.validate()
    saved = config.save(path)
    _log(f"wrote {saved}")
    _log(f"agents will see you as '{config.name}' [{config.session_id[:8]}]")
    _log("start the relay with: pinglucas start")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """Run the relay in the foreground. This process *is* the peer session."""
    config = Config.load(Path(args.config) if args.config else None)
    relay = Relay(config, log=_log)
    ensure_private_dir(state_home())
    write_json(
        _pid_file(),
        {
            "pid": os.getpid(),
            "procStart": proc_start(os.getpid()),
            "session_id": relay.session_id,
            "name": config.name,
            "socket": str(relay.socket_path),
            "startedAt": int(time.time() * 1000),
        },
        0o600,
    )
    try:
        relay.run_forever()
    finally:
        try:
            _pid_file().unlink()
        except OSError:
            pass
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """Start the relay in the background, detached from this terminal."""
    existing = _running()
    if existing:
        _log(f"already running as pid {existing['pid']} ('{existing.get('name')}')")
        return 0
    ensure_private_dir(state_home())
    config = Config.load(Path(args.config) if args.config else None)
    log_path = config.log_path
    command = [sys.executable, "-m", "ping_lucas", "serve"]
    if args.config:
        command += ["--config", args.config]
    with open(log_path, "ab") as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            cwd="/",
        )
    for _ in range(60):
        time.sleep(0.1)
        record = _running()
        if record:
            _log(f"relay running as pid {record['pid']} ('{record.get('name')}')")
            _log(f"log: {log_path}")
            return 0
        if process.poll() is not None:
            break
    _log(f"relay failed to start; see {log_path}")
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    record = _running()
    if not record:
        _log("not running")
        return 0
    pid = int(record["pid"])
    # Only ever signal a process that is still our own relay generation.
    if proc_name(pid) not in ("python3", "python", "python3.12", "ping-lucas", "pinglucas") and not proc_name(pid).startswith("python"):
        _log(f"pid {pid} is no longer a PingLucas relay; refusing to signal it")
        return 1
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        time.sleep(0.1)
        if not _running():
            _log("stopped")
            return 0
    _log("relay did not exit in time")
    return 1


def cmd_status(args: argparse.Namespace) -> int:
    record = _running()
    config = Config.load(Path(args.config) if args.config else None)
    if not record:
        print(f"PingLucas {__version__}: not running")
        print(f"  name        {config.name}")
        print(f"  transports  {', '.join(config.transports)}")
        print("  start with  pinglucas start")
        return 1
    print(f"PingLucas {__version__}: running")
    print(f"  name        {record.get('name')}")
    print(f"  session     {record.get('session_id', '')[:8]}")
    print(f"  pid         {record['pid']}")
    print(f"  socket      {record.get('socket')}")
    print(f"  address     {uds_address(str(record.get('socket', '')))}")
    ledger = Ledger(config.ledger_path, ttl_s=config.ledger_ttl_s)
    pending = ledger.pending()
    print(f"  pending     {len(pending)}")
    for entry in pending[:5]:
        print(f"    [{entry.tag}] {entry.label}: {_short(entry.preview, 50)}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Explain, line by line, whether this machine can carry the relay."""
    config = Config.load(Path(args.config) if args.config else None)
    ok = True

    print(f"PingLucas {__version__}")
    print(f"  config      {config_path()}")
    print(f"  identity    {config.name} [{(config.session_id or '(unset)')[:8]}]")

    print("\nClaude registries")
    roots = config_roots(config.extra_claude_roots)
    if not roots:
        print("  none found — is Claude Code installed for this user?")
        ok = False
    for directory in registry_dirs(config.extra_claude_roots):
        writable = os.access(directory, os.W_OK)
        print(f"  {'ok ' if writable else 'RO '} {directory}")
        ok = ok and writable

    print("\nLive Claude sessions")
    peers = PeerDirectory(config.extra_claude_roots).peers()
    if not peers:
        print("  none — start a Claude session to see it here")
    for peer in peers[:12]:
        print(f"  {peer.label:<28} {peer.session_id[:8]}  {peer.status:<8} {_short(peer.cwd, 40)}")

    print("\nTransports")
    for name in config.transports:
        try:
            transport = build(name, config.transport_settings(name))
            if args.learn_chat and name == "telegram" and not transport.chat_id:
                chat_id = transport.discover_chat_id()
                if chat_id:
                    config.settings.setdefault("telegram", {})["chat_id"] = chat_id
                    config.save()
                    transport.chat_id = chat_id
                    print(f"  learned chat id {chat_id} and saved it")
                else:
                    print("  no chat id found — send the bot a message first")
            status = transport.check()
            print(f"  ok  {name}: {status}")
            if args.ping:
                transport.send_note("PingLucas is wired up correctly.")
                print(f"      sent a test notification via {name}")
        except PingLucasError as exc:
            print(f"  ERR {name}: {exc}")
            ok = False

    print("\nRelay")
    record = _running()
    print(f"  {'running as pid ' + str(record['pid']) if record else 'not running'}")
    return 0 if ok else 1


def cmd_peers(args: argparse.Namespace) -> int:
    config = Config.load(Path(args.config) if args.config else None)
    peers = PeerDirectory(config.extra_claude_roots).peers()
    if not peers:
        print("no live Claude sessions")
        return 1
    width = max(len(p.label) for p in peers)
    for peer in sorted(peers, key=lambda p: p.label.casefold()):
        print(f"{peer.label:<{width}}  {peer.session_id[:8]}  {peer.status:<8} {_short(peer.cwd, 48)}")
    return 0


def cmd_send(args: argparse.Namespace) -> int:
    """Message an agent from the command line, without the relay running."""
    config = Config.load(Path(args.config) if args.config else None)
    directory = PeerDirectory(config.extra_claude_roots)
    record = _running()
    if not record:
        raise PingLucasError("the relay is not running; start it so agents can reply to you")
    outbox = Outbox(
        directory,
        self_address=uds_address(str(record["socket"])),
        self_name=config.name,
    )
    relay_directory = PeerDirectory(config.extra_claude_roots)
    peers = relay_directory.peers()
    selector = args.to.strip()
    matches = [p for p in peers if p.name.casefold() == selector.casefold()] or [
        p for p in peers if p.session_id.startswith(selector) and len(selector) >= 4
    ]
    if not matches:
        raise PingLucasError(f"no live session matches {selector!r}")
    if len(matches) > 1:
        raise PingLucasError(
            f"{selector!r} is ambiguous: " + ", ".join(p.session_id[:8] for p in matches)
        )
    receipt = outbox.send_message(
        matches[0], args.message, priority=args.priority, mode_attestation=config.mode_attestation
    )
    print(json.dumps(receipt, indent=2))
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    config = Config.load(Path(args.config) if args.config else None)
    ledger = Ledger(config.ledger_path, ttl_s=config.ledger_ttl_s)
    entries = ledger.recent(args.limit) if args.all else ledger.pending()
    if not entries:
        print("nothing waiting")
        return 0
    for entry in entries:
        mark = " " if not entry.answered else "✓"
        age = _age(entry.created_at)
        print(f"{mark} [{entry.tag}] {entry.label:<24} {age:>5}  {_short(entry.preview, 60)}")
    return 0


def cmd_reply(args: argparse.Namespace) -> int:
    """Answer a pending question from the terminal instead of the watch."""
    config = Config.load(Path(args.config) if args.config else None)
    record = _running()
    if not record:
        raise PingLucasError("the relay is not running")
    relay = _AttachedRelay(config, record)
    text = args.message if not args.tag else f"{args.tag} {args.message}"
    relay.route_reply(IncomingReply(text=text, received_at=time.time()))
    return 0


def cmd_install_service(args: argparse.Namespace) -> int:
    """Write a systemd --user unit so the relay survives reboots."""
    unit_dir = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    module_root = str(Path(__file__).resolve().parents[1])
    unit = unit_dir / "ping-lucas.service"
    unit.write_text(
        f"""[Unit]
Description=PingLucas relay — publishes Lucas as a Claude Code peer session
After=network-online.target

[Service]
Type=simple
Environment=PYTHONPATH={module_root}
Environment=PYTHONUNBUFFERED=1
ExecStart={sys.executable} -m ping_lucas serve
Restart=always
RestartSec=5
WorkingDirectory={Path.home()}

[Install]
WantedBy=default.target
""",
        encoding="utf-8",
    )
    print(f"wrote {unit}")
    print("enable it with:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable --now ping-lucas.service")
    print("  loginctl enable-linger $USER   # keep it running when logged out")
    return 0


class _AttachedRelay:
    """A relay-shaped object that reuses a running relay's identity for one send.

    Used by ``pinglucas reply`` so the terminal can answer a question without
    a second process trying to claim the same socket.
    """

    def __init__(self, config: Config, record: dict):
        self.config = config
        self.ledger = Ledger(config.ledger_path, ttl_s=config.ledger_ttl_s)
        self.directory = PeerDirectory(config.extra_claude_roots)
        self.outbox = Outbox(
            self.directory,
            self_address=uds_address(str(record["socket"])),
            self_name=config.name,
        )

    route_reply = Relay.route_reply
    resolve_target = Relay.resolve_target
    _outbound_hop_chain = staticmethod(lambda inherited: inherited)
    _notify = staticmethod(lambda text: print(text))
    log = staticmethod(_log)
    replied = 0

    class _Inbox:
        hop_token = ""

    inbox = _Inbox()


def _age(stamp: float) -> str:
    seconds = max(0, int(time.time() - stamp))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# ---------------------------------------------------------------- argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pinglucas",
        description="Publish a human as a Claude Code peer session that agents can message.",
    )
    parser.add_argument("--version", action="version", version=f"PingLucas {__version__}")
    parser.add_argument("--config", help="path to config.json")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="write a config file")
    init.add_argument("--name", default="lucas", help="the name agents will address")
    init.add_argument("--transport", default="telegram", choices=available())
    init.add_argument("--telegram-token")
    init.add_argument("--telegram-chat-id")
    init.add_argument("--ntfy-topic")
    init.add_argument("--ntfy-server")
    init.add_argument("--webhook-url")
    init.set_defaults(func=cmd_init)

    sub.add_parser("serve", help="run the relay in the foreground").set_defaults(func=cmd_serve)
    sub.add_parser("start", help="run the relay in the background").set_defaults(func=cmd_start)
    sub.add_parser("stop", help="stop the background relay").set_defaults(func=cmd_stop)
    sub.add_parser("status", help="show relay status").set_defaults(func=cmd_status)

    doctor = sub.add_parser("doctor", help="diagnose the whole path end to end")
    doctor.add_argument("--learn-chat", action="store_true", help="discover and save a Telegram chat id")
    doctor.add_argument("--ping", action="store_true", help="send a test notification")
    doctor.set_defaults(func=cmd_doctor)

    sub.add_parser("peers", help="list live Claude sessions").set_defaults(func=cmd_peers)

    send = sub.add_parser("send", help="message an agent")
    send.add_argument("to")
    send.add_argument("message")
    send.add_argument("--priority", default="now", choices=("now", "next", "later"))
    send.set_defaults(func=cmd_send)

    pending = sub.add_parser("pending", help="questions waiting for an answer")
    pending.add_argument("--all", action="store_true", help="include answered questions")
    pending.add_argument("--limit", type=int, default=20)
    pending.set_defaults(func=cmd_pending)

    reply = sub.add_parser("reply", help="answer a pending question")
    reply.add_argument("message")
    reply.add_argument("--tag", default="", help="which question (default: the most recent)")
    reply.set_defaults(func=cmd_reply)

    sub.add_parser("install-service", help="write a systemd --user unit").set_defaults(
        func=cmd_install_service
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except PingLucasError as exc:
        print(f"pinglucas: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
