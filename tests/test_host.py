"""The topic/session registry: independent conversations, resume, and idle eviction.

Most of this runs against a host whose `start_proxy` hands out a `FakeProxy`,
so the registry is exercised with no subprocess. The three cases the
implementation guide names as Phase 2's validation run against the real
scripted subprocess instead, in `test_host_end_to_end.py` — a registry that
only ever meets a fake has not been shown to spawn anything.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from r2d2box import AgentConfig, MemoryTranscriptStore

from fake_proxy import FakeHost, FakeProxy, RecordingSubscriber, wait_until


@pytest.fixture
async def host():
    """A `FakeHost` with a trivial config callback, closed at teardown."""
    made = FakeHost(lambda topic, name: AgentConfig(append_system_prompt=f"about {topic}"))
    yield made
    await made.close()


async def run_one_turn(host: FakeHost, topic: str, name: str, text: str) -> str:
    """Submit one prompt and let its turn finish; return what the agent replied."""
    session = await host.session(topic, name)
    turn_id = await session.submit(text)
    proxy = host.proxies[(topic, name)]
    await proxy.run_turn(turn_id, text=f"answer to {text}")
    await proxy.drain()
    return turn_id


# ---- the registry ------------------------------------------------------------


async def test_asking_twice_for_a_session_gives_the_same_one(host):
    first = await host.session("bug-1", "s1")
    assert await host.session("bug-1", "s1") is first


async def test_two_sessions_under_one_topic_hold_independent_conversations(host):
    """Phase 2's first validation, against the fake: two agents, two transcripts."""
    await run_one_turn(host, "bug-1", "s1", "question one")
    await run_one_turn(host, "bug-1", "s2", "question two")

    first = await (await host.session("bug-1", "s1")).snapshot()
    second = await (await host.session("bug-1", "s2")).snapshot()
    assert [turn["user"] for turn in first["turns"]] == ["question one"]
    assert [turn["user"] for turn in second["turns"]] == ["question two"]
    assert len(host.spawns) == 2


async def test_the_same_session_name_under_two_topics_is_two_conversations(host):
    await run_one_turn(host, "bug-1", "s1", "about bug 1")
    await run_one_turn(host, "bug-2", "s1", "about bug 2")

    first = await (await host.session("bug-1", "s1")).snapshot()
    assert [turn["user"] for turn in first["turns"]] == ["about bug 1"]


async def test_creating_a_session_starts_no_process(host):
    await host.session("bug-1", "s1")
    assert host.spawns == []


async def test_create_session_mints_a_name_nothing_else_is_using(host):
    first = await host.create_session("bug-1")
    second = await host.create_session("bug-1")
    assert first.name != second.name
    assert {s.name for s in host.live_sessions("bug-1")} == {first.name, second.name}


# ---- the configuration callback ----------------------------------------------


async def test_the_config_callback_runs_at_every_spawn_not_once_at_mount():
    """DESIGN Decision 5: a prompt built from live data is only current at spawn time."""
    prompts = []

    def agent_config(topic, name):
        prompts.append(f"{topic} as of call {len(prompts) + 1}")
        return AgentConfig(append_system_prompt=prompts[-1])

    host = FakeHost(agent_config)
    try:
        await run_one_turn(host, "bug-1", "s1", "first")
        await (await host.session("bug-1", "s1")).stop_process()
        await run_one_turn(host, "bug-1", "s1", "second")
    finally:
        await host.close()

    assert prompts == ["bug-1 as of call 1", "bug-1 as of call 2"]


async def test_the_config_callback_may_be_async():
    async def agent_config(topic, name):
        await asyncio.sleep(0)
        return AgentConfig(append_system_prompt=f"about {topic}")

    host = FakeHost(agent_config)
    try:
        await run_one_turn(host, "bug-1", "s1", "hello")
        assert len(host.spawns) == 1
    finally:
        await host.close()


async def test_the_spawn_tag_names_the_topic_and_session(host):
    await run_one_turn(host, "bug-1", "s1", "hello")
    assert host.spawns == [("bug-1", "s1", None)]


# ---- listing, for the host's own session picker ------------------------------


async def test_list_sessions_covers_the_stored_and_the_live(host):
    """Decision 3 leaves the picker to the host, so this has to answer for both halves."""
    await run_one_turn(host, "bug-1", "stored", "a question")
    await host.session("bug-1", "brand-new")

    listed = [info.session for info in await host.list_sessions("bug-1")]
    assert set(listed) == {"stored", "brand-new"}


async def test_list_sessions_reports_a_session_in_both_places_once(host):
    await run_one_turn(host, "bug-1", "s1", "a question")
    listed = await host.list_sessions("bug-1")
    assert [info.session for info in listed] == ["s1"]


async def test_list_sessions_survives_a_restart_through_the_store(tmp_path):
    """A host that has just started knows only what the store tells it."""
    store = MemoryTranscriptStore()
    first = FakeHost(lambda t, n: AgentConfig(), store=store)
    await run_one_turn(first, "bug-1", "s1", "before the restart")
    await first.close()

    second = FakeHost(lambda t, n: AgentConfig(), store=store)
    try:
        assert [info.session for info in await second.list_sessions("bug-1")] == ["s1"]
        assert second.live_sessions("bug-1") == []
    finally:
        await second.close()


async def test_list_sessions_puts_the_most_recent_first(host):
    await run_one_turn(host, "bug-1", "older", "one")
    await run_one_turn(host, "bug-1", "newer", "two")
    (await host.session("bug-1", "older")).last_active -= 600

    listed = [info.session for info in await host.list_sessions("bug-1")]
    assert listed == ["newer", "older"]


# ---- closing a session -------------------------------------------------------


async def test_close_session_stops_the_agent_and_keeps_the_transcript(host):
    await run_one_turn(host, "bug-1", "s1", "a question")

    assert await host.close_session("bug-1", "s1") is True

    assert host.live_sessions("bug-1") == []
    revived = await host.session("bug-1", "s1")
    assert [turn["user"] for turn in (await revived.snapshot())["turns"]] == ["a question"]


async def test_close_session_with_clear_discards_the_transcript(host):
    """The `DELETE /sessions` case: the conversation is over, not merely idle."""
    await run_one_turn(host, "bug-1", "s1", "a question")

    await host.close_session("bug-1", "s1", clear=True)

    revived = await host.session("bug-1", "s1")
    assert (await revived.snapshot())["turns"] == []


async def test_closing_a_session_that_does_not_exist_says_so(host):
    assert await host.close_session("bug-1", "never") is False


async def test_closing_the_host_closes_every_session(host):
    await run_one_turn(host, "bug-1", "s1", "one")
    await run_one_turn(host, "bug-2", "s1", "two")

    await host.close()

    assert host.live_sessions() == []
    assert all(not proxy.alive for proxy in host.proxies.values())


# ---- idle eviction -----------------------------------------------------------


async def test_an_idle_session_loses_its_process_and_keeps_everything_else(host):
    host.idle_timeout_s = 60
    await run_one_turn(host, "bug-1", "s1", "a question")
    session = await host.session("bug-1", "s1")
    session.last_active -= 120

    assert await host.evict_idle() == 1

    assert session.process_alive is False
    assert session.claude_session_id == "claude-1"
    assert await host.session("bug-1", "s1") is session
    assert [turn["user"] for turn in (await session.snapshot())["turns"]] == ["a question"]


async def test_a_recently_active_session_is_left_alone(host):
    host.idle_timeout_s = 60
    await run_one_turn(host, "bug-1", "s1", "a question")

    assert await host.evict_idle() == 0
    assert (await host.session("bug-1", "s1")).process_alive is True


async def test_a_session_with_a_turn_in_flight_is_spared(host):
    """Decision 8: a turn runs on with nobody listening, and refreshes no timestamp."""
    host.idle_timeout_s, host.pending_evict_cap_s = 60, 3600
    session = await host.session("bug-1", "s1")
    turn_id = await session.submit("a slow question")
    await host.proxies[("bug-1", "s1")].emit(
        {"type": "turn_start", "turn": {"id": turn_id, "kind": "user"}}
    )
    await host.proxies[("bug-1", "s1")].drain()
    session.last_active -= 120

    assert await host.evict_idle() == 0
    assert session.pending_turns == 1


async def test_the_pending_exemption_runs_out_at_the_cap(host):
    """A `turn_end` that never arrives would otherwise make its session immortal."""
    host.idle_timeout_s, host.pending_evict_cap_s = 60, 300
    session = await host.session("bug-1", "s1")
    turn_id = await session.submit("a question whose end never comes")
    await host.proxies[("bug-1", "s1")].emit(
        {"type": "turn_start", "turn": {"id": turn_id, "kind": "user"}}
    )
    await host.proxies[("bug-1", "s1")].drain()
    session.last_active -= 600

    assert await host.evict_idle() == 1
    assert session.process_alive is False


async def test_an_already_evicted_session_is_not_evicted_again(host):
    host.idle_timeout_s = 60
    await run_one_turn(host, "bug-1", "s1", "a question")
    (await host.session("bug-1", "s1")).last_active -= 120

    assert await host.evict_idle() == 1
    assert await host.evict_idle() == 0


async def test_an_evicted_sessions_next_turn_works(host):
    """Phase 2's third validation: the client never learns the eviction happened."""
    host.idle_timeout_s = 60
    await run_one_turn(host, "bug-1", "s1", "a question")
    session = await host.session("bug-1", "s1")
    session.last_active -= 120
    await host.evict_idle()

    await run_one_turn(host, "bug-1", "s1", "a follow-up")

    assert host.spawns == [("bug-1", "s1", None), ("bug-1", "s1", "claude-1")]
    assert [turn["user"] for turn in (await session.snapshot())["turns"]] == [
        "a question", "a follow-up",
    ]


async def test_the_sweeper_evicts_on_its_own(host):
    host.idle_timeout_s = 60
    await run_one_turn(host, "bug-1", "s1", "a question")
    session = await host.session("bug-1", "s1")
    session.last_active -= 120

    host.start_sweeper(interval_s=0.01)
    deadline = time.monotonic() + 2.0
    while session.process_alive and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    await host.stop_sweeper()

    assert session.process_alive is False


async def test_stopping_a_sweeper_that_never_started_is_safe(host):
    await host.stop_sweeper()


async def test_starting_the_sweeper_twice_leaves_one_running(host):
    host.start_sweeper(interval_s=60)
    first = host._sweeper
    host.start_sweeper(interval_s=60)
    assert host._sweeper is first
    await host.stop_sweeper()


async def test_the_host_works_as_an_async_context_manager():
    async with FakeHost(lambda t, n: AgentConfig()) as host:
        await run_one_turn(host, "bug-1", "s1", "hello")
    assert host.live_sessions() == []


# ---- opening a new conversation ----------------------------------------------


async def opened_turn(host: FakeHost, topic: str, name: str) -> FakeProxy:
    """Wait for the opening turn to reach a process, and return that process."""
    await wait_until(
        lambda: (topic, name) in host.proxies, what="the opening turn spawning an agent"
    )
    proxy = host.proxies[(topic, name)]
    await wait_until(lambda: proxy.submits, what="the opening prompt reaching agent-proxy")
    return proxy


async def test_a_new_session_opens_with_the_hosts_prompt():
    """The host gets to say the first thing, whoever asked for the session."""
    host = FakeHost(opening_prompt=lambda topic, name: f"You are helping with {topic}.")
    try:
        await host.session("bug-1", "s1")
        proxy = await opened_turn(host, "bug-1", "s1")

        assert proxy.submits[0]["text"] == "You are helping with bug-1."
    finally:
        await host.close()


async def test_a_session_created_by_an_attaching_client_opens_the_same_way():
    """The path this was built for: nobody named the session, so nobody could prime it."""
    host = FakeHost(opening_prompt=lambda topic, name: "opening")
    try:
        session = await host.create_session("bug-1")
        proxy = await opened_turn(host, "bug-1", session.name)

        assert proxy.submits[0]["text"] == "opening"
    finally:
        await host.close()


async def test_a_resuming_conversation_is_not_opened_again():
    """The stored transcript is what tells a new conversation from a returning one."""
    store = MemoryTranscriptStore()
    openings = []
    host = FakeHost(store=store, opening_prompt=lambda topic, name: openings.append(name) or "hi")
    try:
        await run_one_turn(host, "bug-1", "s1", "a question")
        await host.close_session("bug-1", "s1")          # keeps the transcript
        openings.clear()

        await host.session("bug-1", "s1")
        await asyncio.sleep(0.05)

        assert openings == [], "the conversation already has history"
    finally:
        await host.close()


async def test_an_opening_prompt_may_be_async_and_may_decline():
    """Returning None is how a host opens some conversations and not others."""
    async def opening_prompt(topic: str, name: str) -> str | None:
        return "opening" if topic == "bug-1" else None

    host = FakeHost(opening_prompt=opening_prompt)
    try:
        await host.session("bug-2", "s1")
        await asyncio.sleep(0.05)
        assert ("bug-2", "s1") not in host.proxies, "no prompt, no process"

        await host.session("bug-1", "s1")
        proxy = await opened_turn(host, "bug-1", "s1")
        assert proxy.submits[0]["text"] == "opening"
    finally:
        await host.close()


async def test_the_opening_prompt_skips_the_build_prompt_hook():
    """It is the host's own words already; wrapping them in a person's context is wrong."""
    host = FakeHost(
        build_prompt=lambda topic, name, text, context: f"[selected text]\n{text}",
        opening_prompt=lambda topic, name: "opening",
    )
    try:
        await host.session("bug-1", "s1")
        proxy = await opened_turn(host, "bug-1", "s1")

        assert proxy.submits[0]["text"] == "opening"
    finally:
        await host.close()


async def test_a_question_typed_straight_away_queues_behind_the_opening_turn():
    """Otherwise the conversation starts with the answer to the wrong thing."""
    host = FakeHost(opening_prompt=lambda topic, name: "opening")
    try:
        session = await host.session("bug-1", "s1")
        await session.submit("what is this about?")

        proxy = host.proxies[("bug-1", "s1")]
        assert [submit["text"] for submit in proxy.submits] == [
            "opening", "what is this about?",
        ]
    finally:
        await host.close()


async def test_an_opening_prompt_that_fails_tells_the_clients_watching():
    """A briefing the agent never got is worse than one the reader knows failed."""
    def opening_prompt(topic: str, name: str) -> str:
        raise RuntimeError("the bug summary could not be read")

    host = FakeHost(opening_prompt=opening_prompt)
    try:
        session = await host.session("bug-1", "s1")
        subscriber = RecordingSubscriber()
        await session.attach(subscriber)
        await wait_until(
            lambda: subscriber.of_type("error"), what="the failure reaching the client"
        )

        assert "could not be read" in subscriber.of_type("error")[0]["error"]
    finally:
        await host.close()


async def test_closing_a_session_cancels_an_opening_that_never_finished():
    """A host shutting down mid-spawn should not be waited on by a turn nobody asked for."""
    async def opening_prompt(topic: str, name: str) -> str:
        await asyncio.sleep(30)
        return "far too late"

    host = FakeHost(opening_prompt=opening_prompt)
    session = await host.session("bug-1", "s1")
    await asyncio.sleep(0)

    await host.close()

    assert ("bug-1", "s1") not in host.proxies
    assert session._opening is None
