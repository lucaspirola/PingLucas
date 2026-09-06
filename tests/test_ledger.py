"""The reply ledger: which agent a wrist-typed answer belongs to."""

from __future__ import annotations

import stat
import time

import pytest

from ping_lucas.ledger import (
    MAX_ENTRIES,
    TAG_ALPHABET,
    TAG_LENGTH,
    Entry,
    Ledger,
    split_tag,
)
from ping_lucas.registry import PeerSession


def peer(session: str = "s-1", name: str = "builder", pid: int = 4242) -> PeerSession:
    return PeerSession(
        session_id=session,
        name=name,
        pid=pid,
        proc_start="123456",
        socket_path="/tmp/cc-socks-1000/4242.sock",
        registry_dir="/home/x/.claude/sessions",
        record_path="/home/x/.claude/sessions/4242.json",
        cwd="/work",
    )


@pytest.fixture
def ledger(tmp_path):
    return Ledger(tmp_path / "state" / "ledger.json")


# ----------------------------------------------------------------- the alphabet


def test_the_tag_alphabet_excludes_characters_that_are_ambiguous_out_loud():
    for ambiguous in "01ilou":
        assert ambiguous not in TAG_ALPHABET, ambiguous
    assert len(TAG_ALPHABET) == len(set(TAG_ALPHABET)) == 30
    assert TAG_LENGTH == 4


def test_generated_tags_only_use_that_alphabet(ledger):
    tags = {ledger.record(peer(), f"m{index}", "body").tag for index in range(40)}
    assert len(tags) == 40, "tags must not collide"
    for tag in tags:
        assert len(tag) == TAG_LENGTH
        assert set(tag) <= set(TAG_ALPHABET)


# -------------------------------------------------------------------- split_tag


@pytest.mark.parametrize(
    "text,expected",
    [
        ("abcd text", ("abcd", "text")),
        ("[abcd] text", ("abcd", "text")),
        ("#abcd: text", ("abcd", "text")),
        ("abcd. two words", ("abcd", "two words")),
        ("abcd, and more", ("abcd", "and more")),
        ("  abcd   padded  ", ("abcd", "padded")),
        ("abcd", ("abcd", "")),
        ("[abcd]", ("abcd", "")),
        ("#abcd:", ("abcd", "")),
        ("no tag at all", ("", "no tag at all")),
        ("hello there", ("", "hello there")),
        ("abcde nope", ("", "abcde nope")),
        ("ab12 short", ("", "ab12 short")),
        ("", ("", "")),
        ("abcd multi\nline body", ("abcd", "multi\nline body")),
    ],
)
def test_split_tag(text, expected):
    assert split_tag(text) == expected


def test_split_tag_rejects_a_tag_using_an_excluded_letter():
    # 'i' is not in the alphabet, so "ship" can never be mistaken for a tag.
    assert split_tag("ship it now") == ("", "ship it now")


# ------------------------------------------------------------------ persistence


def test_entries_survive_a_reload(tmp_path):
    path = tmp_path / "state" / "ledger.json"
    first = Ledger(path)
    entry = first.record(peer(), "m-1", "  should   we   deploy?  ", hop_chain="a" * 24, priority="next")
    first.link_external(entry.tag, "9911")

    second = Ledger(path)
    reloaded = second.get(entry.tag)
    assert reloaded is not None
    assert reloaded == Entry(**{**vars(entry), "external_ref": "9911"})
    assert reloaded.preview == "should we deploy?"
    assert reloaded.priority == "next"
    assert reloaded.hop_chain == "a" * 24
    assert reloaded.to_peer().session_id == "s-1"
    assert reloaded.to_peer().pid == 4242


def test_the_ledger_file_is_private(ledger):
    ledger.record(peer(), "m-1", "body")
    assert stat.S_IMODE(ledger.path.lstat().st_mode) == 0o600
    assert stat.S_IMODE(ledger.path.parent.lstat().st_mode) == 0o700


def test_a_corrupt_entry_is_skipped_not_fatal(tmp_path):
    import json

    path = tmp_path / "state" / "ledger.json"
    good = Ledger(path)
    kept = good.record(peer(), "m-1", "body")

    raw = json.loads(path.read_text())
    raw["entries"].append({"tag": "junk"})  # missing every required field
    raw["entries"].append("not even a dict")
    path.write_text(json.dumps(raw))
    path.chmod(0o600)

    reloaded = Ledger(path)
    assert [e.tag for e in reloaded.recent()] == [kept.tag]


# -------------------------------------------------------------------- eviction


def test_entries_older_than_the_ttl_are_evicted(tmp_path):
    ledger = Ledger(tmp_path / "state" / "ledger.json", ttl_s=60.0)
    stale = ledger.record(peer(), "old", "ancient question")
    fresh = ledger.record(peer(), "new", "current question")
    ledger._entries[stale.tag].created_at = time.time() - 3600

    assert [e.tag for e in ledger.recent()] == [fresh.tag]
    assert ledger.get(stale.tag) is None


def test_max_entries_eviction_drops_the_oldest(tmp_path):
    ledger = Ledger(tmp_path / "state" / "ledger.json", max_entries=2)
    base = time.time() - 100
    tags = []
    for index in range(4):
        entry = ledger.record(peer(), f"m{index}", f"question {index}")
        entry.created_at = base + index
        tags.append(entry.tag)

    surviving = [e.tag for e in ledger.recent()]
    assert len(surviving) == 2
    assert surviving == [tags[3], tags[2]], "newest first, oldest dropped"
    assert ledger.get(tags[0]) is None
    assert ledger.get(tags[1]) is None


def test_the_default_bound_is_generous_but_finite():
    assert MAX_ENTRIES == 200


# --------------------------------------------------------------------- lookup


def test_by_external_finds_the_transport_side_id(ledger):
    first = ledger.record(peer(), "m-1", "first")
    second = ledger.record(peer(), "m-2", "second")
    ledger.link_external(first.tag, "1001")
    ledger.link_external(second.tag, "1002")

    assert ledger.by_external("1002").tag == second.tag
    assert ledger.by_external("1001").tag == first.tag
    assert ledger.by_external("nope") is None
    assert ledger.by_external("") is None


def test_link_external_ignores_an_unknown_tag_and_an_empty_reference(ledger):
    entry = ledger.record(peer(), "m-1", "first")
    ledger.link_external("zzzz", "1001")
    ledger.link_external(entry.tag, "")
    assert ledger.get(entry.tag).external_ref == ""


def test_latest_prefers_an_unanswered_question(tmp_path):
    ledger = Ledger(tmp_path / "state" / "ledger.json")
    base = time.time() - 100
    older = ledger.record(peer(), "m-1", "older, still waiting")
    newer = ledger.record(peer(), "m-2", "newer, already answered")
    ledger._entries[older.tag].created_at = base
    ledger._entries[newer.tag].created_at = base + 10

    assert ledger.latest().tag == newer.tag
    ledger.mark_answered(newer.tag)
    assert ledger.get(newer.tag).answered is True

    assert [e.tag for e in ledger.pending()] == [older.tag]
    assert ledger.latest().tag == older.tag, "an unanswered question outranks a newer answered one"

    ledger.mark_answered(older.tag)
    assert ledger.pending() == []
    assert ledger.latest().tag == newer.tag, "with nothing pending, fall back to the most recent"


def test_latest_is_none_on_an_empty_ledger(ledger):
    assert ledger.latest() is None
    assert ledger.pending() == []
    assert ledger.recent() == []
