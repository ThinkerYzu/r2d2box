"""Transcript storage: the shared contract, and what the file store does to a key."""

from __future__ import annotations

import json

import pytest

from r2d2box import FileTranscriptStore, MemoryTranscriptStore, Turn
from r2d2box.store import _slug


@pytest.fixture(params=["memory", "file"])
def store(request, tmp_path):
    """Both implementations, so every contract test runs against each."""
    if request.param == "memory":
        return MemoryTranscriptStore()
    return FileTranscriptStore(tmp_path / "transcripts")


def a_turn(turn_id: str, user: str = "hello", *, by_host: bool = False) -> Turn:
    """One finished turn, as a session would hand it to a store."""
    return Turn(
        id=turn_id,
        kind="user",
        user=user,
        by_host=by_host,
        events=[{"type": "text", "turn": {"id": turn_id}, "text": "hi"}],
        ended_at=1000.0,
        outcome="success",
    )


# ---- the contract both stores must meet -------------------------------------


async def test_turns_come_back_in_the_order_they_were_appended(store):
    await store.append_turn("bug-1", "s1", a_turn("t-1", "first"))
    await store.append_turn("bug-1", "s1", a_turn("t-2", "second"))

    turns = await store.read_turns("bug-1", "s1")
    assert [turn.user for turn in turns] == ["first", "second"]
    assert turns[0].events[0]["text"] == "hi"


async def test_reading_a_session_that_was_never_written_is_empty(store):
    assert await store.read_turns("bug-1", "never") == []


async def test_sessions_under_one_topic_do_not_mix(store):
    await store.append_turn("bug-1", "s1", a_turn("t-1", "in s1"))
    await store.append_turn("bug-1", "s2", a_turn("t-1", "in s2"))

    assert [t.user for t in await store.read_turns("bug-1", "s1")] == ["in s1"]
    assert [t.user for t in await store.read_turns("bug-1", "s2")] == ["in s2"]


async def test_topics_do_not_mix(store):
    await store.append_turn("bug-1", "s1", a_turn("t-1", "one"))
    await store.append_turn("bug-2", "s1", a_turn("t-1", "two"))

    assert [t.user for t in await store.read_turns("bug-1", "s1")] == ["one"]
    assert [t.user for t in await store.read_turns("bug-2", "s1")] == ["two"]


async def test_list_sessions_names_every_stored_session(store):
    await store.append_turn("bug-1", "s1", a_turn("t-1"))
    await store.append_turn("bug-1", "s2", a_turn("t-1"))
    await store.append_turn("bug-2", "other", a_turn("t-1"))

    assert {info.session for info in await store.list_sessions("bug-1")} == {"s1", "s2"}
    assert [info.session for info in await store.list_sessions("bug-2")] == ["other"]
    assert await store.list_sessions("bug-3") == []


async def test_list_sessions_counts_the_turns_of_each(store):
    await store.append_turn("bug-1", "s1", a_turn("t-1"))
    await store.append_turn("bug-1", "s1", a_turn("t-2"))
    await store.append_turn("bug-1", "s2", a_turn("t-1"))

    counts = {info.session: info.turns for info in await store.list_sessions("bug-1")}
    assert counts == {"s1": 2, "s2": 1}


async def test_list_sessions_previews_the_first_question(store):
    await store.append_turn("bug-1", "s1", a_turn("t-1", "why does it crash?"))
    await store.append_turn("bug-1", "s1", a_turn("t-2", "and on Wayland?"))

    listed = await store.list_sessions("bug-1")
    assert [info.preview for info in listed] == ["why does it crash?"]


async def test_the_preview_skips_a_turn_the_host_submitted(store):
    """A conversation opened by the host is labelled by the person, not the opening."""
    await store.append_turn("bug-1", "s1", a_turn("t-1", "here is the bug", by_host=True))
    await store.append_turn("bug-1", "s1", a_turn("t-2", "why does it crash?"))

    listed = await store.list_sessions("bug-1")
    assert [info.preview for info in listed] == ["why does it crash?"]
    assert [info.turns for info in listed] == [2]


async def test_a_conversation_with_only_a_host_turn_has_no_preview(store):
    await store.append_turn("bug-1", "s1", a_turn("t-1", "here is the bug", by_host=True))

    assert [info.preview for info in await store.list_sessions("bug-1")] == [None]


async def test_the_preview_is_one_short_line(store):
    await store.append_turn(
        "bug-1", "s1", a_turn("t-1", "  why does\n\nit crash? " + "x" * 200)
    )

    (info,) = await store.list_sessions("bug-1")
    assert info.preview.startswith("why does it crash? x")
    assert len(info.preview) == 120


async def test_clear_discards_one_session_and_leaves_the_rest(store):
    await store.append_turn("bug-1", "s1", a_turn("t-1"))
    await store.append_turn("bug-1", "s2", a_turn("t-1"))

    await store.clear("bug-1", "s1")

    assert await store.read_turns("bug-1", "s1") == []
    assert len(await store.read_turns("bug-1", "s2")) == 1
    assert [info.session for info in await store.list_sessions("bug-1")] == ["s2"]


async def test_clearing_a_session_that_was_never_written_is_not_an_error(store):
    await store.clear("bug-1", "never")


# ---- Turn's JSON form --------------------------------------------------------


def test_a_turn_survives_the_round_trip_through_json():
    turn = a_turn("t-1", "why does it crash?")
    restored = Turn.from_dict(json.loads(json.dumps(turn.to_dict())))
    assert restored == turn


def test_a_turn_written_by_an_older_version_still_loads():
    """Missing keys take their defaults and unknown ones are dropped."""
    restored = Turn.from_dict({"id": "t-1", "events": [], "invented_later": 7})
    assert restored.id == "t-1"
    assert restored.kind == "user"
    assert restored.user is None
    assert restored.by_host is False
    assert restored.outcome is None


# ---- what the file store does with a key -------------------------------------


async def test_a_hostile_topic_key_cannot_escape_the_root(tmp_path):
    """A topic is any string the host likes, and none of them may pick the path."""
    root = tmp_path / "transcripts"
    store = FileTranscriptStore(root)

    await store.append_turn("../../etc/passwd", "s1", a_turn("t-1"))

    written = list(root.rglob("*.jsonl"))
    assert len(written) == 1
    assert root in written[0].parents
    assert ".." not in str(written[0].relative_to(root))


async def test_two_topics_that_slug_alike_stay_apart(tmp_path):
    """Collapsing and trimming are lossy, so the digest is what keeps keys distinct."""
    store = FileTranscriptStore(tmp_path / "transcripts")
    await store.append_turn("bug 1/2", "s1", a_turn("t-1", "slashed"))
    await store.append_turn("bug 1-2", "s1", a_turn("t-1", "dashed"))

    assert [t.user for t in await store.read_turns("bug 1/2", "s1")] == ["slashed"]
    assert [t.user for t in await store.read_turns("bug 1-2", "s1")] == ["dashed"]


async def test_list_sessions_recovers_the_key_the_caller_used(tmp_path):
    """The file name is a slug; the session id in it has to come back unmangled."""
    store = FileTranscriptStore(tmp_path / "transcripts")
    await store.append_turn("Bug 1992198: crash on resize", "Tab 2 / draft", a_turn("t-1"))

    listed = await store.list_sessions("Bug 1992198: crash on resize")
    assert [info.session for info in listed] == ["Tab 2 / draft"]


async def test_list_sessions_ignores_a_file_that_is_not_ours(tmp_path):
    """A stray `.jsonl` in the topic directory has no header and names no session."""
    root = tmp_path / "transcripts"
    store = FileTranscriptStore(root)
    await store.append_turn("bug-1", "s1", a_turn("t-1"))
    (root / _slug("bug-1") / "stray.jsonl").write_text("not our file\n")

    assert [info.session for info in await store.list_sessions("bug-1")] == ["s1"]


async def test_a_corrupt_line_costs_one_turn_and_not_the_transcript(tmp_path):
    """A process that died mid-append leaves a partial last line; the rest still loads."""
    root = tmp_path / "transcripts"
    store = FileTranscriptStore(root)
    await store.append_turn("bug-1", "s1", a_turn("t-1", "first"))
    await store.append_turn("bug-1", "s1", a_turn("t-2", "second"))
    path = root / _slug("bug-1") / f"{_slug('s1')}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"id": "t-3", "user": "trunc')

    assert [t.user for t in await store.read_turns("bug-1", "s1")] == ["first", "second"]


async def test_a_corrupt_line_is_still_counted_by_a_listing(tmp_path):
    """The count is a line count, so it disagrees with `read_turns` by the bad line.

    Documented rather than fixed: agreeing exactly would mean parsing every
    line of every transcript under the topic each time a picker is drawn.
    """
    root = tmp_path / "transcripts"
    store = FileTranscriptStore(root)
    await store.append_turn("bug-1", "s1", a_turn("t-1", "first"))
    path = root / _slug("bug-1") / f"{_slug('s1')}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"id": "t-2", "user": "trunc')

    (info,) = await store.list_sessions("bug-1")
    assert info.turns == 2
    assert len(await store.read_turns("bug-1", "s1")) == 1


async def test_a_listing_reads_past_a_corrupt_line_for_its_preview(tmp_path):
    """A bad line is stepped over, not taken as the end of the search."""
    root = tmp_path / "transcripts"
    store = FileTranscriptStore(root)
    await store.append_turn("bug-1", "s1", a_turn("t-1", "first"))
    path = root / _slug("bug-1") / f"{_slug('s1')}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    path.write_text(lines[0] + "{not json\n" + lines[1], encoding="utf-8")

    (info,) = await store.list_sessions("bug-1")
    assert info.preview == "first"


async def test_a_transcript_outlives_the_store_object(tmp_path):
    """Two `FileTranscriptStore`s on one root are the same transcript, as after a restart."""
    root = tmp_path / "transcripts"
    await FileTranscriptStore(root).append_turn("bug-1", "s1", a_turn("t-1", "before"))

    turns = await FileTranscriptStore(root).read_turns("bug-1", "s1")
    assert [turn.user for turn in turns] == ["before"]


def test_a_slug_stays_readable_and_stays_unique():
    slug = _slug("Bug 1992198: crash on resize")
    assert slug.startswith("Bug-1992198-crash-on-resize-")
    assert _slug("") != _slug(" ")
    assert "/" not in _slug("a/b") and ".." not in _slug("..")
