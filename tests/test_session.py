"""One conversation: turn correlation, fan-out, the task set, and losing the process."""

from __future__ import annotations

import asyncio

import pytest

from r2d2box import MemoryTranscriptStore, ProxyStartError, SubmitRejected

from fake_proxy import FakeProxy, FakeSpawner, RecordingSubscriber, wait_until


def turn_ref(turn_id: str, kind: str = "user") -> dict:
    """The `turn` object every message of one turn carries."""
    return {"id": turn_id, "kind": kind}


# ---- submitting a turn -------------------------------------------------------


async def test_submit_returns_the_turn_id_the_ack_claimed(started_session):
    session, proxy, turn_id = await started_session()
    assert turn_id == "t-1"
    assert proxy.submits[0]["text"] == "what host is this?"


async def test_the_ref_is_what_claims_the_turn_and_never_reaches_the_client(fake_session):
    """Counting acks goes off by one on the first rejection, so the ref does the binding."""
    session, spawner = fake_session()
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)

    turn_id = await session.submit("hello")
    proxy = spawner.latest
    await proxy.run_turn(turn_id)
    await proxy.drain()

    assert proxy.submits[0]["ref"] == "r2d2-1"
    assert "ack" not in subscriber.types()
    assert not any("ref" in message for message in subscriber.messages)


async def test_a_rejected_submit_raises_instead_of_waiting_for_a_turn(fake_session):
    """agent-proxy answers a bad prompt with an `error` and no ack — nothing is coming."""
    session, spawner = fake_session()
    await session.submit("first")           # starts the process
    proxy = spawner.latest
    proxy.auto_ack = False

    pending = asyncio.ensure_future(session.submit("   "))
    await asyncio.sleep(0)
    await proxy.reject("r2d2-2")

    with pytest.raises(SubmitRejected, match="non-empty string"):
        await pending


async def test_two_turns_in_one_session_get_different_ids(fake_session):
    session, spawner = fake_session()
    first = await session.submit("one")
    proxy = spawner.latest
    await proxy.run_turn(first)
    await proxy.drain()
    second = await session.submit("two")

    assert first != second
    assert [submit["ref"] for submit in proxy.submits] == ["r2d2-1", "r2d2-2"]


async def test_submitting_to_a_closed_session_raises(fake_session):
    session, _ = fake_session()
    await session.close()
    with pytest.raises(ConnectionError, match="closed"):
        await session.submit("too late")


# ---- the prompt hook ---------------------------------------------------------


async def test_build_prompt_rewrites_what_the_agent_is_asked(fake_session):
    calls = []

    def build_prompt(topic, name, text, context):
        calls.append((topic, name, text, context))
        return f"<{context['file']}>\n{text}"

    session, spawner = fake_session(build_prompt=build_prompt)
    await session.submit("explain this", context={"file": "DESIGN.md"})

    assert calls == [("topic-1", "s1", "explain this", {"file": "DESIGN.md"})]
    assert spawner.latest.submits[0]["text"] == "<DESIGN.md>\nexplain this"


async def test_build_prompt_may_be_async(fake_session):
    """open question: a host that looks something up mid-assembly."""

    async def build_prompt(topic, name, text, context):
        await asyncio.sleep(0)
        return f"looked up: {text}"

    session, spawner = fake_session(build_prompt=build_prompt)
    await session.submit("why?")
    assert spawner.latest.submits[0]["text"] == "looked up: why?"


async def test_the_transcript_records_what_was_typed_not_what_was_sent(fake_session):
    """A host may prepend a document to every prompt; the transcript must not show it."""
    session, spawner = fake_session(build_prompt=lambda t, n, text, c: f"CONTEXT\n{text}")
    turn_id = await session.submit("what changed?")
    await spawner.latest.run_turn(turn_id)
    await spawner.latest.drain()

    turns = (await session.snapshot())["turns"]
    assert [turn["user"] for turn in turns] == ["what changed?"]


async def test_a_failing_prompt_hook_fails_the_submit(fake_session):
    """Sending the bare text would get a confident answer to a question nobody asked."""

    def build_prompt(topic, name, text, context):
        raise RuntimeError("the database is down")

    session, spawner = fake_session(build_prompt=build_prompt)
    with pytest.raises(RuntimeError, match="database is down"):
        await session.submit("hello")
    assert spawner.resumes == []  # no process was started for a prompt that never formed


# ---- the envelope ------------------------------------------------------------


async def test_every_message_carries_topic_session_and_a_gapless_seq(started_session):
    session, proxy, turn_id = await started_session()
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)
    await proxy.run_turn(turn_id)
    await proxy.drain()

    assert all(m["topic"] == "topic-1" and m["session"] == "s1" for m in subscriber.messages)
    seqs = [message["seq"] for message in subscriber.messages]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))


async def test_the_proxys_own_seq_is_kept_under_another_name(started_session):
    """It restarts at 1 with every process, so it cannot number the conversation."""
    session, proxy, turn_id = await started_session()
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)
    sent = await proxy.emit({"type": "text", "turn": turn_ref(turn_id), "text": "hi"})
    await proxy.drain()

    forwarded = subscriber.of_type("text")[0]
    assert forwarded["proxy_seq"] == sent["seq"]
    assert forwarded["seq"] != sent["seq"]


async def test_the_message_itself_passes_through_unchanged(started_session):
    """An envelope is added; the vocabulary is not rewritten."""
    session, proxy, turn_id = await started_session()
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)
    await proxy.emit({
        "type": "tool_use",
        "turn": turn_ref(turn_id),
        "tool": "Read",
        "input": {"file_path": "/etc/hostname"},
        "tool_use_id": "tu-1",
    })
    await proxy.drain()

    tool_use = subscriber.of_type("tool_use")[0]
    assert tool_use["tool"] == "Read"
    assert tool_use["input"] == {"file_path": "/etc/hostname"}
    assert tool_use["turn"] == turn_ref(turn_id)
    assert "outstanding" in tool_use


# ---- fan-out and attaching ---------------------------------------------------


async def test_every_attached_client_sees_the_same_stream(started_session):
    """— neither app this replaces does this today."""
    session, proxy, turn_id = await started_session()
    first, second = RecordingSubscriber(), RecordingSubscriber()
    await session.attach(first)
    await session.attach(second)

    await proxy.run_turn(turn_id, text="the host is r2d2")
    await proxy.drain()

    assert first.types()[1:] == ["turn_start", "text", "turn_end"]
    assert second.types()[1:] == ["turn_start", "text", "turn_end"]
    assert first.of_type("text")[0]["text"] == second.of_type("text")[0]["text"]


async def test_attach_delivers_the_transcript_before_anything_live(started_session):
    """A late joiner reads one ordered conversation, not a live tail with a gap in it."""
    session, proxy, first_turn = await started_session()
    await proxy.run_turn(first_turn, text="answered")
    await proxy.drain()

    subscriber = RecordingSubscriber()
    await session.attach(subscriber)
    second_turn = await session.submit("and now?")
    await proxy.run_turn(second_turn, text="still here")
    await proxy.drain()

    attached = subscriber.messages[0]
    assert attached["type"] == "attached"
    assert [turn["user"] for turn in attached["turns"]] == ["what host is this?"]
    assert subscriber.types()[1:] == ["turn_prompt", "turn_start", "text", "turn_end"]


async def test_a_client_that_did_not_submit_still_learns_what_was_asked(started_session):
    """The other tabs get the question, or they watch an answer to nothing."""
    session, proxy, _ = await started_session()
    watcher = RecordingSubscriber()
    await session.attach(watcher)

    turn_id = await session.submit("and now?")

    prompt = watcher.of_type("turn_prompt")[0]
    assert prompt["text"] == "and now?"
    assert prompt["turn"] == turn_ref(turn_id)
    assert prompt["seq"] == watcher.messages[0]["seq"] + 1


async def test_the_prompt_is_broadcast_but_not_recorded_as_an_event(started_session):
    """`Turn.user` already carries it; recording it too would draw it twice on replay."""
    session, proxy, turn_id = await started_session()
    await proxy.run_turn(turn_id, text="r2d2")
    await proxy.drain()

    stored = (await session.snapshot())["turns"][-1]
    assert stored["user"] == "what host is this?"
    assert "turn_prompt" not in [event["type"] for event in stored["events"]]


async def test_a_message_racing_an_attach_arrives_exactly_once(started_session):
    """The lock's whole job: the client sees a racing message in one place, not both or neither.

    Which place depends on who takes the lock first, and the test does not
    care — that is the point. Snapshot outside the lock and this message is
    lost; send the transcript after releasing it and the message arrives ahead
    of the transcript it belongs after.
    """
    session, proxy, turn_id = await started_session()
    await proxy.emit({"type": "turn_start", "turn": turn_ref(turn_id)})
    await proxy.drain()

    subscriber = RecordingSubscriber()
    await asyncio.gather(
        session.attach(subscriber),
        proxy.emit({"type": "text", "turn": turn_ref(turn_id), "text": "racing"}),
    )
    await proxy.drain()

    attached = subscriber.messages[0]
    assert attached["type"] == "attached"
    in_snapshot = [
        event
        for turn in attached["turns"]
        for event in turn["events"]
        if event.get("text") == "racing"
    ]
    live = [m for m in subscriber.messages[1:] if m.get("text") == "racing"]
    assert len(in_snapshot) + len(live) == 1


async def test_a_turn_in_flight_is_in_the_snapshot_before_it_is_stored(started_session):
    """a client that reconnects mid-turn finds the turn, not a hole."""
    session, proxy, turn_id = await started_session()
    await proxy.emit({"type": "turn_start", "turn": turn_ref(turn_id)})
    await proxy.emit({"type": "text", "turn": turn_ref(turn_id), "text": "working"})
    await proxy.drain()

    snapshot = await session.snapshot()
    assert snapshot["turn_active"] is True
    assert [turn["id"] for turn in snapshot["turns"]] == [turn_id]
    assert [event["type"] for event in snapshot["turns"][0]["events"]] == [
        "turn_start", "text",
    ]


async def test_a_turn_appears_once_when_it_finishes_mid_snapshot(started_session):
    """A finished turn leaves `_open_turns` for the store, and is never in both."""
    session, proxy, turn_id = await started_session()
    await proxy.run_turn(turn_id)
    await proxy.drain()

    snapshot = await session.snapshot()
    assert [turn["id"] for turn in snapshot["turns"]] == [turn_id]
    assert snapshot["turn_active"] is False


async def test_detaching_stops_delivery_and_leaves_the_others(started_session):
    session, proxy, turn_id = await started_session()
    staying, leaving = RecordingSubscriber(), RecordingSubscriber()
    await session.attach(staying)
    await session.attach(leaving)
    await session.detach(leaving)

    await proxy.run_turn(turn_id)
    await proxy.drain()

    assert "turn_end" in staying.types()
    assert leaving.types() == ["attached"]


async def test_a_subscriber_that_fails_is_dropped_and_the_stream_carries_on(started_session):
    session, proxy, turn_id = await started_session()
    good = RecordingSubscriber()
    await session.attach(good)
    session._subscribers.add(RecordingSubscriber(fail=True))

    await proxy.run_turn(turn_id)
    await proxy.drain()

    assert "turn_end" in good.types()
    assert session.subscriber_count == 1


async def test_a_turn_runs_to_completion_with_nobody_attached(started_session):
    """from the other side: the transcript does not need an audience."""
    session, proxy, turn_id = await started_session()
    await proxy.run_turn(turn_id, text="nobody heard this")
    await proxy.drain()

    snapshot = await session.snapshot()
    assert snapshot["turns"][0]["outcome"] == "success"
    assert snapshot["turns"][0]["events"][1]["text"] == "nobody heard this"


# ---- turns nobody submitted, and messages that name none ---------------------


async def test_an_unowned_turn_is_recorded_and_forwarded(started_session):
    """The inner claude starts turns on its own; they have no ack and no user text."""
    session, proxy, turn_id = await started_session()
    await proxy.run_turn(turn_id)
    await proxy.drain()
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)

    await proxy.run_turn("t-bg", kind="unowned", text="a background task finished")
    await proxy.drain()

    assert subscriber.types()[1:] == ["turn_start", "text", "turn_end"]
    stored = (await session.snapshot())["turns"]
    assert [turn["kind"] for turn in stored] == ["user", "unowned"]
    assert stored[1]["user"] is None


async def test_a_tool_result_with_no_turn_lands_in_the_running_turn(started_session):
    """agent-proxy runs one turn at a time, so `the` running turn is not a guess."""
    session, proxy, turn_id = await started_session()
    await proxy.emit({"type": "turn_start", "turn": turn_ref(turn_id)})
    await proxy.emit({"type": "tool_result", "tool_use_id": "tu-1", "content": "r2d2"})
    await proxy.emit({
        "type": "turn_end", "turn": turn_ref(turn_id),
        "basis": "marker:turn_duration", "outcome": "success",
    })
    await proxy.drain()

    events = (await session.snapshot())["turns"][0]["events"]
    assert [event["type"] for event in events] == ["turn_start", "tool_result", "turn_end"]


async def test_a_session_error_with_nothing_running_is_sent_but_stored_nowhere(fake_session):
    session, spawner = fake_session()
    turn_id = await session.submit("hello")
    proxy = spawner.latest
    await proxy.run_turn(turn_id)
    await proxy.drain()
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)

    await proxy.emit({"type": "error", "error": "inner claude went quiet"})
    await proxy.drain()

    assert subscriber.types()[1:] == ["error"]
    stored = (await session.snapshot())["turns"]
    assert len(stored) == 1
    assert all(event["type"] != "error" for event in stored[0]["events"])


# ---- the background-task set -------------------------------------------------


async def test_the_task_set_tracks_task_start_and_task_end(started_session):
    session, proxy, turn_id = await started_session()
    await proxy.emit({"type": "task_start", "turn": turn_ref(turn_id),
                      "task": {"id": "bash_3"}, "summary": "build firefox"})
    await proxy.drain()
    assert session.task_ids == {"bash_3"}

    await proxy.emit({"type": "task_end", "turn": turn_ref(turn_id),
                      "task": {"id": "bash_3"}, "exit_code": 0})
    await proxy.drain()
    assert session.task_ids == set()


async def test_a_task_that_ends_with_nobody_connected_still_clears(started_session):
    """The bug one original fixed the hard way: a stale client set blocks the composer."""
    session, proxy, turn_id = await started_session()
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)
    await proxy.emit({"type": "task_start", "turn": turn_ref(turn_id), "task": {"id": "bash_3"}})
    await proxy.drain()

    await session.detach(subscriber)
    await proxy.emit({"type": "task_end", "turn": turn_ref(turn_id),
                      "task": {"id": "bash_3"}, "exit_code": 0})
    await proxy.drain()

    await session.attach(subscriber)
    assert subscriber.messages[-1]["task_ids"] == []


async def test_attach_reports_the_tasks_still_running(started_session):
    session, proxy, turn_id = await started_session()
    await proxy.emit({"type": "task_start", "turn": turn_ref(turn_id), "task": {"id": "bash_3"}})
    await proxy.emit({"type": "task_start", "turn": turn_ref(turn_id), "task": {"id": "bash_9"}})
    await proxy.drain()

    subscriber = RecordingSubscriber()
    await session.attach(subscriber)
    assert subscriber.messages[0]["task_ids"] == ["bash_3", "bash_9"]


async def test_the_outstanding_counts_are_replaced_not_accumulated(started_session):
    """They are absolute, so a session that missed messages is right again on the next."""
    session, proxy, turn_id = await started_session()
    await proxy.emit({"type": "text", "turn": turn_ref(turn_id), "text": "a",
                      "outstanding": {"user": 1, "unowned": 0, "background": 2}})
    await proxy.drain()
    assert (await session.status())["outstanding"] == {"user": 1, "unowned": 0, "background": 2}

    await proxy.emit({"type": "text", "turn": turn_ref(turn_id), "text": "b",
                      "outstanding": {"user": 1, "unowned": 0, "background": 0}})
    await proxy.drain()
    assert (await session.status())["outstanding"]["background"] == 0


# ---- losing the process ------------------------------------------------------


async def test_a_process_that_dies_mid_turn_ends_the_turn_with_an_error(started_session):
    session, proxy, turn_id = await started_session()
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)
    await proxy.emit({"type": "turn_start", "turn": turn_ref(turn_id)})
    await proxy.drain()

    await proxy.end_stream()
    await wait_until(
        lambda: "process_exited" in subscriber.types(), what="the process_exited message"
    )

    ended = subscriber.of_type("turn_end")[0]
    assert ended["outcome"] == "error" and ended["basis"] == "error"
    stored = (await session.snapshot())["turns"]
    assert stored[0]["outcome"] == "error"


async def test_a_dead_process_leaves_no_turn_pending(started_session):
    """`pending_turns` must fall back to zero or the session is exempt from eviction forever."""
    session, proxy, turn_id = await started_session()
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)
    await proxy.emit({"type": "turn_start", "turn": turn_ref(turn_id)})
    await proxy.drain()
    assert session.pending_turns == 1

    # `process_exited` is the last thing the read pump sends on its way out, so
    # seeing it means everything the exit path does has already happened.
    await proxy.end_stream()
    await wait_until(
        lambda: "process_exited" in subscriber.types(), what="the process_exited message"
    )

    assert session.pending_turns == 0
    assert session.turn_active is False
    assert session.process_alive is False


async def test_a_process_that_dies_before_the_ack_wakes_the_submit(fake_session):
    session, spawner = fake_session()
    await session.submit("first")
    proxy = spawner.latest
    proxy.auto_ack = False

    pending = asyncio.ensure_future(session.submit("second"))
    await asyncio.sleep(0)
    await proxy.end_stream()

    with pytest.raises(ConnectionError, match="exited"):
        await pending


# ---- resuming ----------------------------------------------------------------


async def test_the_first_spawn_asks_for_no_resume(fake_session):
    session, spawner = fake_session()
    await session.submit("hello")
    assert spawner.resumes == [None]
    assert session.claude_session_id == "fake-session-0001"


async def test_the_next_turn_after_an_eviction_resumes_the_conversation(fake_session):
    session, spawner = fake_session()
    first = await session.submit("what host is this?")
    await spawner.latest.run_turn(first, text="r2d2")
    await spawner.latest.drain()

    await session.stop_process()
    assert session.process_alive is False

    await session.submit("and the kernel?")

    assert spawner.resumes == [None, "fake-session-0001"]
    assert session.process_alive is True
    # The stored turn and the one just acknowledged, in that order: the new
    # process picked up a conversation rather than starting one.
    assert [turn["user"] for turn in (await session.snapshot())["turns"]] == [
        "what host is this?", "and the kernel?",
    ]


async def test_a_transient_resume_failure_is_retried_against_the_same_id(fake_session):
    """Dropping a good id on one bad spawn starts a second conversation over the first."""
    spawner = FakeSpawner(errors=(None, ProxyStartError("boom")))
    session, _ = fake_session(spawner=spawner)
    await session.submit("first")
    await session.stop_process()

    await session.submit("second")

    assert spawner.resumes == [None, "fake-session-0001", "fake-session-0001"]
    assert session.claude_session_id == "fake-session-0002"


async def test_a_resume_that_keeps_failing_gives_up_and_starts_fresh(fake_session):
    """A lost history beats a session that can never talk again."""
    spawner = FakeSpawner(
        errors=(None, ProxyStartError("gone"), ProxyStartError("gone")),
        session_ids=("old-session", "new-session"),
    )
    session, _ = fake_session(spawner=spawner)
    await session.submit("first")
    await session.stop_process()

    await session.submit("second")

    assert spawner.resumes == [None, "old-session", "old-session", None]
    assert session.claude_session_id == "new-session"


async def test_a_spawn_that_never_works_raises_to_the_caller(fake_session):
    spawner = FakeSpawner(errors=(ProxyStartError("no agent-proxy on PATH"),))
    session, _ = fake_session(spawner=spawner)
    with pytest.raises(ProxyStartError, match="no agent-proxy"):
        await session.submit("hello")


async def test_two_submits_at_once_start_one_process(fake_session):
    """A second submit racing the first must join its agent, not spawn a rival."""
    session, spawner = fake_session()
    await asyncio.gather(session.submit("one"), session.submit("two"))
    assert len(spawner.proxies) == 1


# ---- shutdown ----------------------------------------------------------------


async def test_closing_stores_a_turn_that_was_still_running(fake_session):
    store = MemoryTranscriptStore()
    session, spawner = fake_session(store=store)
    turn_id = await session.submit("hello")
    await spawner.latest.emit({"type": "turn_start", "turn": turn_ref(turn_id)})
    await spawner.latest.drain()

    await session.close()

    stored = await store.read_turns("topic-1", "s1")
    assert [turn.outcome for turn in stored] == ["error"]


async def test_closing_twice_is_safe(fake_session):
    session, _ = fake_session()
    await session.submit("hello")
    await session.close()
    await session.close()


async def test_closing_tells_the_clients_watching_before_it_drops_them(started_session):
    """A tab that only watches has no other way to learn its conversation is gone."""
    session, _, _ = await started_session()
    subscriber = RecordingSubscriber()
    await session.attach(subscriber)

    await session.close()

    assert subscriber.types()[-1] == "session_closed"
    assert session.subscriber_count == 0


async def test_a_closed_session_leaves_its_transcript_behind(fake_session):
    """The store outlives the object, so the same conversation can be picked back up."""
    store = MemoryTranscriptStore()
    session, spawner = fake_session(store=store)
    turn_id = await session.submit("hello")
    await spawner.latest.run_turn(turn_id)
    await spawner.latest.drain()

    await session.close()

    assert len(await store.read_turns("topic-1", "s1")) == 1


async def test_clear_discards_the_conversation(fake_session):
    session, spawner = fake_session()
    turn_id = await session.submit("hello")
    await spawner.latest.run_turn(turn_id)
    await spawner.latest.drain()

    await session.clear()

    assert (await session.snapshot())["turns"] == []


async def test_status_reports_the_live_state_without_the_transcript(started_session):
    session, proxy, turn_id = await started_session()
    await proxy.emit({"type": "turn_start", "turn": turn_ref(turn_id)})
    await proxy.emit({"type": "task_start", "turn": turn_ref(turn_id), "task": {"id": "bash_3"}})
    await proxy.drain()

    status = await session.status()
    assert status["turn_active"] is True
    assert status["turn_ids"] == [turn_id]
    assert status["task_ids"] == ["bash_3"]
    assert status["process_alive"] is True
    assert "turns" not in status


async def test_the_fake_proxy_matches_the_real_ones_surface():
    """`FakeProxy` is only a valid stand-in while it answers everything a session asks."""
    from r2d2box import AgentProxy

    surface = ["session_id", "alive", "returncode", "submit", "request_status",
               "messages", "close"]
    missing = [member for member in surface if not hasattr(FakeProxy(), member)]
    assert missing == []
    assert all(hasattr(AgentProxy, member) for member in surface)
