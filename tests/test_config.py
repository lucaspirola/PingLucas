"""Configuration: file, environment overrides, validation and derived paths."""

from __future__ import annotations

import json
import os
import stat

import pytest

from ping_lucas.config import Config, config_home, config_path, state_home
from ping_lucas.errors import ConfigError


def test_defaults_are_usable_without_a_config_file(tmp_path):
    config = Config.load(tmp_path / "missing.json")
    assert config.name == "lucas"
    assert config.transports == ["console"]
    assert config.echo_tag is True
    assert config.mode_attestation == "bypass"
    assert config.refresh_interval_s == 5.0


def test_paths_follow_the_xdg_variables(tmp_path):
    assert config_home() == tmp_path / "config" / "ping-lucas"
    assert state_home() == tmp_path / "state" / "ping-lucas"
    assert config_path() == tmp_path / "config" / "ping-lucas" / "config.json"


def test_ping_lucas_config_overrides_the_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PING_LUCAS_CONFIG", str(tmp_path / "elsewhere.json"))
    assert config_path() == tmp_path / "elsewhere.json"


# --------------------------------------------------------------- environment


def test_environment_overrides_apply(tmp_path, monkeypatch):
    monkeypatch.setenv("PING_LUCAS_NAME", "lp")
    monkeypatch.setenv("PING_LUCAS_SESSION_ID", "fixed-session")
    monkeypatch.setenv("PING_LUCAS_TRANSPORT", " telegram , console ")
    monkeypatch.setenv("PING_LUCAS_TELEGRAM_TOKEN", "123:abc")
    monkeypatch.setenv("PING_LUCAS_TELEGRAM_CHAT_ID", "-4242")
    monkeypatch.setenv("PING_LUCAS_NTFY_TOPIC", "secret-topic")
    monkeypatch.setenv("PING_LUCAS_WEBHOOK_URL", "https://example.invalid/hook")
    monkeypatch.setenv(
        "PING_LUCAS_CLAUDE_CONFIG_DIRS", os.pathsep.join([str(tmp_path / "a"), str(tmp_path / "b")])
    )

    config = Config.load(tmp_path / "missing.json")
    assert config.name == "lp"
    assert config.session_id == "fixed-session"
    assert config.transports == ["telegram", "console"]
    assert config.settings["telegram"] == {"token": "123:abc", "chat_id": "-4242"}
    assert config.settings["ntfy"] == {"topic": "secret-topic"}
    assert config.settings["webhook"] == {"url": "https://example.invalid/hook"}
    assert config.extra_claude_roots == [str(tmp_path / "a"), str(tmp_path / "b")]


def test_an_env_supplied_claude_root_is_not_duplicated_on_every_load_and_save(
    tmp_path, monkeypatch
):
    """``doctor --learn-chat`` and ``init`` both load-then-save the config.

    With PING_LUCAS_CLAUDE_CONFIG_DIRS set, each of those cycles used to append
    the same root again, so the stored list grew without bound.
    """
    monkeypatch.setenv("PING_LUCAS_CLAUDE_CONFIG_DIRS", "/opt/claude")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"extra_claude_roots": []}))
    path.chmod(0o600)

    for _ in range(4):
        Config.load(path).save(path)

    assert json.loads(path.read_text())["extra_claude_roots"] == ["/opt/claude"]


def test_an_environment_override_does_not_clobber_a_sibling_setting(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"settings": {"telegram": {"chat_id": "77"}}}))
    path.chmod(0o600)
    monkeypatch.setenv("PING_LUCAS_TELEGRAM_TOKEN", "123:abc")

    config = Config.load(path)
    assert config.settings["telegram"] == {"chat_id": "77", "token": "123:abc"}


# --------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "name", ["", "a b", 'quote"name', "with/slash", "back\\slash", "<angle>", "x" * 49, "tab\tname"]
)
def test_an_invalid_peer_name_is_rejected(name):
    config = Config(name=name)
    with pytest.raises(ConfigError, match="invalid peer name"):
        config.validate()


def test_a_valid_peer_name_is_accepted():
    for name in ("lucas", "lucas-pirola", "lucas_2", "x" * 48):
        Config(name=name).validate()


@pytest.mark.parametrize("value", ["yolo", "BYPASS", "danger-full-access", "none"])
def test_an_invalid_mode_attestation_is_rejected(value):
    with pytest.raises(ConfigError, match="mode_attestation"):
        Config(mode_attestation=value).validate()


def test_the_three_accepted_mode_attestations():
    for value in ("bypass", "prompting", ""):
        Config(mode_attestation=value).validate()


def test_an_empty_transport_list_is_rejected():
    with pytest.raises(ConfigError, match="at least one transport"):
        Config(transports=[]).validate()


def test_intervals_are_clamped_to_something_survivable():
    config = Config(refresh_interval_s=0.01, heartbeat_interval_s=0.5)
    config.validate()
    assert config.refresh_interval_s == 1.0
    assert config.heartbeat_interval_s == 1.0, "a heartbeat may never be faster than a refresh"


def test_ensure_session_id_is_stable_once_set():
    config = Config()
    first = config.ensure_session_id()
    assert first and config.ensure_session_id() == first


# -------------------------------------------------------------- persistence


def test_save_and_load_roundtrip_preserves_every_setting(tmp_path):
    original = Config(
        name="lp",
        session_id="s-1",
        cwd="/work",
        transports=["ntfy", "webhook"],
        settings={"ntfy": {"topic": "t", "reply_topic": "t-reply"}},
        refresh_interval_s=9.0,
        heartbeat_interval_s=90.0,
        ledger_ttl_s=1800.0,
        extra_claude_roots=["/opt/claude"],
        echo_tag=False,
        mode_attestation="prompting",
    )
    path = original.save(tmp_path / "state" / "config.json")
    assert stat.S_IMODE(path.lstat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.lstat().st_mode) == 0o700

    reloaded = Config.load(path)
    assert reloaded.as_dict() == original.as_dict()


def test_save_defaults_to_the_configured_path(tmp_path):
    Config(name="lp").save()
    assert config_path().is_file()
    assert json.loads(config_path().read_text())["name"] == "lp"


def test_a_world_writable_config_is_ignored_rather_than_trusted(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"name": "attacker"}))
    path.chmod(0o666)
    assert Config.load(path).name == "lucas"


# ----------------------------------------------------------- derived paths


def test_transport_settings_inject_the_paths_each_transport_needs():
    config = Config(settings={"telegram": {"token": "t"}})

    telegram = config.transport_settings("telegram")
    assert telegram["token"] == "t"
    assert telegram["offset_path"] == str(state_home() / "telegram-offset.json")
    assert telegram["offset_path"] == str(config.offset_path)

    assert config.transport_settings("ntfy")["since_path"] == str(
        state_home() / "ntfy-since.json"
    )
    assert config.transport_settings("console")["dir"] == str(state_home() / "console")
    assert config.transport_settings("webhook") == {}


def test_transport_settings_never_mutate_the_stored_settings():
    config = Config(settings={"telegram": {"token": "t"}})
    config.transport_settings("telegram")["offset_path"] = "/tmp/hijack"
    assert config.settings["telegram"] == {"token": "t"}


def test_an_explicit_offset_path_is_not_overridden():
    config = Config(settings={"telegram": {"offset_path": "/tmp/mine.json"}})
    assert config.transport_settings("telegram")["offset_path"] == "/tmp/mine.json"


def test_the_derived_state_paths_all_live_under_the_state_home():
    config = Config()
    for path in (config.ledger_path, config.offset_path, config.log_path):
        assert path.parent == state_home()
