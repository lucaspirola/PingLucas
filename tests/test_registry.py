"""Publication into, and discovery out of, Claude's session registries.

A wrong answer here does not produce a stack trace -- it produces Lucas's words
arriving at somebody else's process.  So the interesting assertions are about
digests, file modes, process generations and refusals.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from _support import bound_socket, peer_record, publish_peer, write_private_json
from ping_lucas import PEER_PROTOCOL
from ping_lucas.errors import RegistryError
from ping_lucas.identity import node_path_resolve, proc_start
from ping_lucas.registry import (
    MAX_SOCKET_PATH_BYTES,
    PeerDirectory,
    RecordPublisher,
    choose_socket_path,
    config_roots,
    key_filename,
    parse_uds_address,
    uds_address,
)

TOKEN = "0123456789abcdef0123456789abcdef"
SESSION = "11111111-1111-4111-8111-111111111111"


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


# --------------------------------------------------------------- key filename


def test_key_filename_digest_is_the_sha256_of_the_resolved_path():
    # Hand-computed: sha256("/run/user/1000/cc-socks/4242.sock")
    expected = "1bee33e60ccd43d6398becd630eb49e9850a4ac16f85832ad38e9ae71f7ae4a0"
    assert key_filename(4242, "/run/user/1000/cc-socks/4242.sock") == f"4242.{expected}.key"
    assert (
        hashlib.sha256(b"/run/user/1000/cc-socks/4242.sock").hexdigest() == expected
    )


def test_key_filename_normalises_lexically_but_never_follows_symlinks(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)

    through_link = link / "9.sock"
    through_real = real / "9.sock"
    assert through_link.resolve() == through_real  # the filesystem agrees they are one file

    # ...but Claude hashes the *lexical* path, so the digests must differ.
    assert key_filename(9, str(through_link)) != key_filename(9, str(through_real))
    assert key_filename(9, str(through_link)) == (
        f"9.{hashlib.sha256(str(through_link).encode()).hexdigest()}.key"
    )


def test_key_filename_collapses_redundant_path_segments():
    assert key_filename(7, "/tmp/./a/../cc-socks/7.sock") == key_filename(7, "/tmp/cc-socks/7.sock")


def test_uds_address_roundtrip():
    path = "/tmp/cc socks/7.sock"
    address = uds_address(path)
    assert address.startswith("uds:") and " " not in address
    assert parse_uds_address(address) == path
    assert parse_uds_address("relative") == ""
    assert parse_uds_address("uds:not/absolute") == ""
    assert parse_uds_address(None) == ""


# ------------------------------------------------------------------ publisher


@pytest.fixture
def publisher(registry_root, socket_dir):
    published = RecordPublisher(
        session_id=SESSION,
        name="lucas",
        socket_path=socket_dir / f"{os.getpid()}.sock",
        token=TOKEN,
        cwd="/work",
        extra_roots=[str(registry_root)],
    )
    yield published
    published.withdraw()


def test_refresh_writes_both_artifacts_with_the_expected_names_and_modes(
    publisher, sessions_dir
):
    written = publisher.refresh()
    assert written == [sessions_dir / f"{os.getpid()}.json"]

    record_path = sessions_dir / f"{os.getpid()}.json"
    key_path = sessions_dir / key_filename(os.getpid(), str(publisher.socket_path))
    assert record_path.is_file() and key_path.is_file()
    assert mode(record_path) == 0o600
    assert mode(key_path) == 0o600
    assert mode(sessions_dir) == 0o700
    assert sorted(p.name for p in sessions_dir.iterdir()) == sorted(
        [record_path.name, key_path.name]
    )


def test_refresh_writes_the_key_before_the_record(publisher, monkeypatch):
    """A record is an invitation; an invitation whose key is absent must not exist."""
    import ping_lucas.registry as registry_module

    order: list[str] = []
    original = registry_module.write_json

    def spy(path, value, mode_bits=0o600):
        order.append("key" if str(path).endswith(".key") else "record")
        return original(path, value, mode_bits)

    monkeypatch.setattr(registry_module, "write_json", spy)
    publisher.refresh()
    assert order == ["key", "record"]


def test_published_record_binds_this_process_generation(publisher, sessions_dir):
    publisher.refresh()
    import json

    record = json.loads((sessions_dir / f"{os.getpid()}.json").read_text())

    assert record["peerProtocol"] == PEER_PROTOCOL == 1
    assert record["pid"] == os.getpid()
    assert record["procStart"] == proc_start(os.getpid()) != ""
    assert record["status"] in {"busy", "shell", "idle", "waiting"}
    assert record["kind"] in {"interactive", "bg", "daemon", "daemon-worker"}
    assert record["sessionId"] == SESSION
    assert record["messagingSocketPath"] == str(publisher.socket_path)
    assert record["pingLucas"]["relay"] is True
    assert record["pingLucas"]["instance"] == publisher.instance_id


def test_published_key_matches_the_record_generation(publisher, sessions_dir):
    import json

    publisher.refresh()
    key = json.loads(
        (sessions_dir / key_filename(os.getpid(), str(publisher.socket_path))).read_text()
    )
    record = json.loads((sessions_dir / f"{os.getpid()}.json").read_text())
    assert key["peerToken"] == TOKEN
    assert key["procStart"] == record["procStart"]
    assert key.get("pidDomain") == record.get("pidDomain")


def test_set_status_rewrites_the_record(publisher, sessions_dir):
    import json

    publisher.refresh()
    publisher.set_status("waiting")
    record = json.loads((sessions_dir / f"{os.getpid()}.json").read_text())
    assert record["status"] == "waiting"


def test_publisher_refuses_a_process_it_cannot_pin(monkeypatch, registry_root, socket_dir):
    import ping_lucas.registry as registry_module

    monkeypatch.setattr(registry_module, "proc_start", lambda pid: "")
    with pytest.raises(RegistryError):
        RecordPublisher(
            session_id=SESSION,
            name="lucas",
            socket_path=socket_dir / "x.sock",
            token=TOKEN,
            extra_roots=[str(registry_root)],
        )


# ------------------------------------------------------------------- withdraw


def test_withdraw_removes_only_our_own_artifacts(publisher, sessions_dir):
    publisher.refresh()
    record_path = sessions_dir / f"{os.getpid()}.json"
    key_path = sessions_dir / key_filename(os.getpid(), str(publisher.socket_path))
    publisher.withdraw()
    assert not record_path.exists()
    assert not key_path.exists()


def test_withdraw_leaves_a_successors_record_in_place(publisher, sessions_dir):
    publisher.refresh()
    record_path = sessions_dir / f"{os.getpid()}.json"
    key_path = sessions_dir / key_filename(os.getpid(), str(publisher.socket_path))

    successor = publisher.record()
    successor["pingLucas"] = dict(successor["pingLucas"], instance="a-different-relay")
    write_private_json(record_path, successor)
    write_private_json(key_path, {"peerToken": "f" * 32, "procStart": publisher.proc_start})

    publisher.withdraw()
    assert record_path.exists(), "a live successor's record must survive our exit"
    assert key_path.exists(), "and so must its key"


def test_withdraw_leaves_a_record_from_another_process_generation(publisher, sessions_dir):
    publisher.refresh()
    record_path = sessions_dir / f"{os.getpid()}.json"
    stale = publisher.record()
    stale["procStart"] = str(int(publisher.proc_start) + 1)
    write_private_json(record_path, stale)

    publisher.withdraw()
    assert record_path.exists()


# ------------------------------------------------------------- peer directory


@pytest.fixture
def live_peer(sessions_dir, socket_dir):
    """A registry entry for *this* process, pointing at a real listening socket."""
    pid = os.getpid()
    socket_path = socket_dir / f"{pid}.sock"
    listener = bound_socket(socket_path)
    record_path, key_path = publish_peer(sessions_dir, pid, socket_path, TOKEN)
    try:
        yield {"pid": pid, "socket": socket_path, "record": record_path, "key": key_path}
    finally:
        listener.close()


def directory() -> PeerDirectory:
    return PeerDirectory()


def test_a_well_formed_record_loads(live_peer, sessions_dir):
    peer = directory().load_record(live_peer["record"])
    assert peer is not None
    assert peer.pid == os.getpid()
    assert peer.socket_path == str(live_peer["socket"])
    assert peer.address == uds_address(str(live_peer["socket"]))
    assert peer.key_path == sessions_dir / key_filename(peer.pid, peer.socket_path)
    assert directory().by_pid(os.getpid()) is not None
    assert directory().peer_token(peer) == TOKEN


def test_a_filename_that_disagrees_with_the_pid_field_is_rejected(live_peer, sessions_dir):
    forged = sessions_dir / "999999.json"
    write_private_json(forged, peer_record(os.getpid(), live_peer["socket"]))
    assert directory().load_record(forged) is None


def test_a_stale_proc_start_is_rejected(live_peer, sessions_dir):
    stale = peer_record(os.getpid(), live_peer["socket"])
    stale["procStart"] = str(int(stale["procStart"]) + 1)
    write_private_json(live_peer["record"], stale)
    assert directory().load_record(live_peer["record"]) is None


def test_a_record_pointing_at_a_non_socket_is_rejected(live_peer, socket_dir, sessions_dir):
    plain = socket_dir / "not-a-socket"
    plain.write_text("")
    plain.chmod(0o600)
    write_private_json(live_peer["record"], peer_record(os.getpid(), plain))
    assert directory().load_record(live_peer["record"]) is None


def test_a_world_readable_socket_is_rejected(live_peer):
    live_peer["socket"].chmod(0o666)
    assert directory().load_record(live_peer["record"]) is None


def test_a_record_in_a_group_writable_directory_is_rejected(live_peer, sessions_dir):
    assert directory().load_record(live_peer["record"]) is not None
    sessions_dir.chmod(0o770)
    try:
        assert directory().load_record(live_peer["record"]) is None
        assert directory().peers() == []
    finally:
        sessions_dir.chmod(0o700)


def test_a_group_readable_record_file_is_rejected(live_peer):
    live_peer["record"].chmod(0o644)
    # 0644 is still fine (it is not group *writable*)...
    assert directory().load_record(live_peer["record"]) is not None
    live_peer["record"].chmod(0o664)
    assert directory().load_record(live_peer["record"]) is None


def test_a_ping_lucas_marker_makes_a_record_invisible(live_peer):
    marked = peer_record(os.getpid(), live_peer["socket"])
    marked["pingLucas"] = {"relay": True, "instance": "abc"}
    write_private_json(live_peer["record"], marked)
    assert directory().load_record(live_peer["record"]) is None
    assert directory().peers() == []


def test_our_own_publication_is_never_returned_as_a_peer(publisher, sessions_dir):
    publisher.refresh()
    assert PeerDirectory().peers() == []
    assert PeerDirectory(self_pid=os.getpid()).peers() == []


def test_a_wrong_protocol_version_is_rejected(live_peer):
    record = peer_record(os.getpid(), live_peer["socket"], peerProtocol=2)
    write_private_json(live_peer["record"], record)
    assert directory().load_record(live_peer["record"]) is None


def test_self_pid_is_excluded(live_peer):
    assert PeerDirectory(self_pid=os.getpid()).load_record(live_peer["record"]) is None


def test_refresh_revalidates_and_notices_a_vanished_session(live_peer):
    peer = directory().load_record(live_peer["record"])
    assert directory().refresh(peer) is not None
    live_peer["record"].unlink()
    assert directory().refresh(peer) is None


def test_peer_token_refuses_a_mismatched_generation_or_shape(live_peer, sessions_dir):
    peer = directory().load_record(live_peer["record"])

    write_private_json(live_peer["key"], {"peerToken": "not-hex", "procStart": peer.proc_start})
    with pytest.raises(RegistryError):
        directory().peer_token(peer)

    write_private_json(
        live_peer["key"], {"peerToken": TOKEN, "procStart": str(int(peer.proc_start) + 1)}
    )
    with pytest.raises(RegistryError):
        directory().peer_token(peer)

    live_peer["key"].chmod(0o644)
    with pytest.raises(RegistryError):
        directory().peer_token(peer)


def test_config_roots_ignores_a_root_without_a_private_sessions_dir(tmp_path, monkeypatch):
    open_root = tmp_path / "open"
    (open_root / "sessions").mkdir(parents=True)
    (open_root / "sessions").chmod(0o777)  # world-writable: anyone could plant a record
    monkeypatch.setenv("PING_LUCAS_CLAUDE_CONFIG_DIRS", str(open_root))
    assert config_roots() == []


# ------------------------------------------------------------- socket choosing


def test_choose_socket_path_uses_the_runtime_dir(runtime_dir):
    chosen = choose_socket_path(4242)
    assert chosen == Path(node_path_resolve(str(runtime_dir / "cc-socks" / "4242.sock")))


def test_choose_socket_path_falls_back_to_tmp_for_an_over_long_runtime_dir(monkeypatch, tmp_path):
    deep = tmp_path / ("d" * 40) / ("e" * 40) / ("f" * 40)
    deep.mkdir(parents=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(deep))
    assert len(os.fsencode(deep / "cc-socks" / "4242.sock")) > MAX_SOCKET_PATH_BYTES

    chosen = choose_socket_path(4242)
    assert chosen == Path(f"/tmp/cc-socks-{os.getuid()}/4242.sock")
    assert len(os.fsencode(chosen)) <= MAX_SOCKET_PATH_BYTES


def test_choose_socket_path_does_not_second_guess_an_explicit_directory(tmp_path):
    deep = tmp_path / ("d" * 40) / ("e" * 40) / ("f" * 40)
    with pytest.raises(RegistryError):
        choose_socket_path(4242, deep)


def test_choose_socket_path_raises_when_even_the_fallback_is_too_long(monkeypatch, tmp_path):
    import ping_lucas.registry as registry_module

    monkeypatch.setattr(registry_module, "MAX_SOCKET_PATH_BYTES", 8)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    with pytest.raises(RegistryError, match="exceeds the Unix socket limit"):
        choose_socket_path(4242)
