"""The ``pinglucas`` command line."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from _support import bound_socket, publish_peer
from ping_lucas import __version__
from ping_lucas.config import Config, config_path, state_home
from ping_lucas.identity import proc_start
from ping_lucas.ledger import Ledger
from ping_lucas.registry import PeerSession
from ping_lucas import cli

TOKEN = "0123456789abcdef0123456789abcdef"

DOCUMENTED_COMMANDS = [
    "init",
    "serve",
    "start",
    "stop",
    "status",
    "doctor",
    "peers",
    "qr",
    "send",
    "pending",
    "reply",
    "install-service",
]


# ------------------------------------------------------------------- parser


def test_build_parser_accepts_every_documented_subcommand():
    parser = cli.build_parser()
    for command in DOCUMENTED_COMMANDS:
        args = parser.parse_args([command] + _minimum_arguments(command))
        assert args.command == command
        assert callable(args.func)


def _minimum_arguments(command: str) -> list[str]:
    if command == "send":
        return ["builder", "hello"]
    if command == "reply":
        return ["yes"]
    return []


def test_the_parser_exposes_no_undocumented_subcommands():
    parser = cli.build_parser()
    actions = [a for a in parser._actions if hasattr(a, "choices") and a.dest == "command"]
    assert sorted(actions[0].choices) == sorted(DOCUMENTED_COMMANDS)


def test_a_subcommand_is_required():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([])


def test_version_is_reported(capsys):
    with pytest.raises(SystemExit) as caught:
        cli.build_parser().parse_args(["--version"])
    assert caught.value.code == 0
    assert __version__ in capsys.readouterr().out


def test_optional_flags_parse():
    parser = cli.build_parser()
    assert parser.parse_args(["doctor", "--learn-chat", "--ping"]).learn_chat is True
    assert parser.parse_args(["pending", "--all", "--limit", "5"]).limit == 5
    assert parser.parse_args(["send", "a", "b", "--priority", "later"]).priority == "later"
    assert parser.parse_args(["reply", "yes", "--tag", "a3f2"]).tag == "a3f2"
    assert parser.parse_args(["--config", "/x.json", "status"]).config == "/x.json"


def test_an_unknown_transport_choice_is_refused():
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["init", "--transport", "smoke-signal"])


# -------------------------------------------------------------------- peers


@pytest.fixture
def live_claude_session(sessions_dir, socket_dir):
    """A registry entry that ``pinglucas peers`` should print."""
    pid = os.getpid()
    socket_path = socket_dir / f"{pid}.sock"
    listener = bound_socket(socket_path)
    publish_peer(
        sessions_dir,
        pid,
        socket_path,
        TOKEN,
        name="api-builder",
        session_id="abcdef12-0000-4000-8000-000000000000",
        status="busy",
        cwd="/work/api",
    )
    try:
        yield socket_path
    finally:
        listener.close()


def test_peers_lists_a_live_session(live_claude_session, capsys):
    assert cli.main(["peers"]) == 0
    out = capsys.readouterr().out
    assert "api-builder" in out
    assert "abcdef12" in out
    assert "busy" in out
    assert "/work/api" in out


def test_peers_reports_an_empty_registry(registry_root, capsys):
    assert cli.main(["peers"]) == 1
    assert "no live Claude sessions" in capsys.readouterr().out


def test_peers_ignores_a_record_whose_socket_has_gone(live_claude_session, capsys):
    live_claude_session.unlink()
    assert cli.main(["peers"]) == 1
    assert "no live Claude sessions" in capsys.readouterr().out


# ------------------------------------------------------------------ _age


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0s"),
        (1, "1s"),
        (59, "59s"),
        (60, "1m"),
        (119, "1m"),
        (3599, "59m"),
        (3600, "1h"),
        (86399, "23h"),
        (86400, "1d"),
        (86400 * 9 + 5, "9d"),
        (-30, "0s"),
    ],
)
def test_age_formatting(seconds, expected, monkeypatch):
    monkeypatch.setattr(cli.time, "time", lambda: 1_000_000.0)
    assert cli._age(1_000_000.0 - seconds) == expected


def test_short_collapses_whitespace_and_elides():
    assert cli._short("  a   b  ") == "a b"
    assert cli._short("x" * 100, width=10) == "x" * 9 + "…"
    assert cli._short("x" * 10, width=10) == "x" * 10


# ------------------------------------------------------------- pid tracking


def test_running_is_none_without_a_pid_file():
    assert cli._running() is None


def test_running_ignores_a_record_from_a_dead_generation(tmp_path):
    from ping_lucas.safeio import ensure_private_dir, write_json

    ensure_private_dir(state_home())
    write_json(
        cli._pid_file(),
        {"pid": os.getpid(), "procStart": str(int(proc_start(os.getpid())) + 1)},
        0o600,
    )
    assert cli._running() is None


def test_running_accepts_our_own_live_generation():
    from ping_lucas.safeio import ensure_private_dir, write_json

    ensure_private_dir(state_home())
    write_json(
        cli._pid_file(),
        {
            "pid": os.getpid(),
            "procStart": proc_start(os.getpid()),
            "name": "lucas",
            "session_id": "s-1",
            "socket": "/tmp/cc-socks-1000/1.sock",
        },
        0o600,
    )
    record = cli._running()
    assert record is not None and record["name"] == "lucas"


def test_is_relay_process_only_matches_a_real_serve_argv():
    # This test process is not `python -m ping_lucas serve`.
    assert cli._is_relay_process(os.getpid()) is False
    assert cli._is_relay_process(999_999_999) is False


def test_stop_is_a_no_op_when_nothing_is_running(capsys):
    assert cli.main(["stop"]) == 0
    assert "not running" in capsys.readouterr().out


# ------------------------------------------------------------------ status


def test_status_reports_a_stopped_relay(registry_root, capsys):
    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out
    assert "not running" in out
    assert "pinglucas start" in out


def test_status_reports_a_running_relay_and_its_backlog(registry_root, capsys):
    from ping_lucas.safeio import ensure_private_dir, write_json

    ensure_private_dir(state_home())
    write_json(
        cli._pid_file(),
        {
            "pid": os.getpid(),
            "procStart": proc_start(os.getpid()),
            "name": "lucas",
            "session_id": "abcdef12-0000-4000-8000-000000000000",
            "socket": "/tmp/cc-socks-1000/1.sock",
        },
        0o600,
    )
    ledger = Ledger(Config().ledger_path)
    entry = ledger.record(
        PeerSession(
            session_id="s",
            name="builder",
            pid=1,
            proc_start="1",
            socket_path="/x",
            registry_dir="/y",
            record_path="/z",
        ),
        "m-1",
        "should I deploy?",
    )

    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "running" in out
    assert "abcdef12" in out
    assert f"[{entry.tag}]" in out
    assert "should I deploy?" in out
    assert "uds:/tmp/cc-socks-1000/1.sock" in out


# ----------------------------------------------------------------- pending


def test_pending_reports_an_empty_ledger(capsys):
    assert cli.main(["pending"]) == 0
    assert "nothing waiting" in capsys.readouterr().out


def test_pending_marks_answered_entries_only_with_all(capsys):
    ledger = Ledger(Config().ledger_path)
    peer = PeerSession(
        session_id="s",
        name="builder",
        pid=1,
        proc_start="1",
        socket_path="/x",
        registry_dir="/y",
        record_path="/z",
    )
    waiting = ledger.record(peer, "m-1", "still waiting")
    answered = ledger.record(peer, "m-2", "already answered")
    ledger.mark_answered(answered.tag)

    assert cli.main(["pending"]) == 0
    out = capsys.readouterr().out
    assert waiting.tag in out and answered.tag not in out

    assert cli.main(["pending", "--all"]) == 0
    out = capsys.readouterr().out
    assert waiting.tag in out and answered.tag in out
    assert "✓" in out


# -------------------------------------------------------------------- init


def test_init_writes_an_ntfy_config_with_generated_topics(capsys):
    assert cli.main(["init", "--transport", "ntfy", "--name", "lp"]) == 0
    saved = json.loads(config_path().read_text())
    assert saved["name"] == "lp"
    assert saved["transports"] == ["ntfy"]
    topic = saved["settings"]["ntfy"]["topic"]
    assert topic.startswith("pinglucas-") and len(topic) > 16, "topics must be unguessable"
    assert saved["settings"]["ntfy"]["reply_topic"] == f"{topic}-reply"
    assert saved["session_id"]
    assert topic in capsys.readouterr().out


def test_init_is_idempotent_about_the_session_id(capsys):
    cli.main(["init", "--transport", "ntfy"])
    first = json.loads(config_path().read_text())
    cli.main(["init", "--transport", "ntfy"])
    second = json.loads(config_path().read_text())
    assert first["session_id"] == second["session_id"]
    assert first["settings"]["ntfy"]["topic"] == second["settings"]["ntfy"]["topic"]


def test_init_refuses_a_webhook_without_a_url(capsys):
    assert cli.main(["init", "--transport", "webhook"]) == 2
    assert "webhook transport needs --webhook-url" in capsys.readouterr().err


def test_init_refuses_telegram_without_a_token(capsys):
    assert cli.main(["init", "--transport", "telegram"]) == 2
    assert "@BotFather" in capsys.readouterr().err


def test_init_refuses_an_invalid_name(capsys):
    assert cli.main(["init", "--transport", "ntfy", "--name", "two words"]) == 2
    assert "invalid peer name" in capsys.readouterr().err


def test_init_honours_an_explicit_config_path(tmp_path, capsys):
    target = tmp_path / "state" / "custom.json"
    assert cli.main(["--config", str(target), "init", "--transport", "ntfy"]) == 0
    assert target.is_file()
    assert not config_path().exists()


# ------------------------------------------------------------------ doctor


def test_doctor_walks_the_whole_path(live_claude_session, monkeypatch, capsys):
    monkeypatch.setenv("PING_LUCAS_TRANSPORT", "console")
    assert cli.main(["doctor", "--ping"]) == 0
    out = capsys.readouterr().out
    assert "Claude registries" in out
    assert "api-builder" in out
    assert "ok  console" in out
    assert "sent a test notification via console" in out
    assert "not running" in out

    note = json.loads((state_home() / "console" / "pings.jsonl").read_text().splitlines()[0])
    assert note["agent"] == "relay"


def test_doctor_fails_when_no_registry_exists(monkeypatch, capsys):
    monkeypatch.setenv("PING_LUCAS_TRANSPORT", "console")
    assert cli.main(["doctor"]) == 1
    assert "is Claude Code installed" in capsys.readouterr().out


def test_doctor_reports_a_broken_transport(monkeypatch, registry_root, capsys):
    monkeypatch.setenv("PING_LUCAS_TRANSPORT", "telegram")
    assert cli.main(["doctor"]) == 1
    assert "ERR telegram" in capsys.readouterr().out


# ------------------------------------------------------------------- misc


def test_send_and_reply_refuse_to_run_without_the_relay(capsys):
    assert cli.main(["send", "builder", "hi"]) == 2
    assert "relay is not running" in capsys.readouterr().err
    assert cli.main(["reply", "yes"]) == 2
    assert "relay is not running" in capsys.readouterr().err


def test_install_service_writes_a_user_unit(tmp_path, capsys):
    assert cli.main(["install-service"]) == 0
    unit = Path(os.environ["XDG_CONFIG_HOME"]) / "systemd" / "user" / "ping-lucas.service"
    text = unit.read_text()
    assert "ExecStart=" in text and "-m ping_lucas serve" in text
    assert "Restart=always" in text
    assert str(Path(cli.__file__).resolve().parents[1]) in text
    assert str(unit) in capsys.readouterr().out


def test_qr_refuses_when_there_is_nothing_to_scan(monkeypatch, capsys):
    monkeypatch.setenv("PING_LUCAS_TRANSPORT", "console")
    assert cli.main(["qr"]) == 2
    assert "nothing to scan" in capsys.readouterr().err


def test_qr_prints_the_ntfy_urls(monkeypatch, capsys):
    monkeypatch.setenv("PING_LUCAS_TRANSPORT", "ntfy")
    monkeypatch.setenv("PING_LUCAS_NTFY_TOPIC", "topic-abc")
    monkeypatch.setenv("PING_LUCAS_NTFY_REPLY_TOPIC", "topic-abc-reply")
    assert cli.main(["qr"]) == 0
    out = capsys.readouterr().out
    assert "https://ntfy.sh/topic-abc" in out
    assert "https://ntfy.sh/topic-abc-reply" in out


def test_a_keyboard_interrupt_exits_with_the_conventional_code(monkeypatch):
    def interrupt(args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_peers", interrupt)
    parser = cli.build_parser()
    original = parser.parse_args

    def parse(argv):
        args = original(argv)
        args.func = interrupt
        return args

    monkeypatch.setattr(cli, "build_parser", lambda: type("P", (), {"parse_args": staticmethod(parse)})())
    assert cli.main(["peers"]) == 130
