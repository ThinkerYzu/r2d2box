"""Shared fixtures: the scripted subprocess, and sessions built on the fake proxy.

Two seams, one per layer. `scripted_config` and `start_scripted` point an
`AgentConfig` at a real subprocess, which is what makes the buffer and shutdown
behavior in `proxy.py` testable at all. `fake_session` goes above that seam and
builds a `Session` on `tests/fake_proxy.py`, so the conversation layer is
exercised with no process anywhere.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from r2d2box import AgentConfig, AgentProxy, MemoryTranscriptStore, Session

from fake_proxy import FakeProxy, FakeSpawner

TESTS_DIR = Path(__file__).parent
FIXTURES_DIR = TESTS_DIR / "fixtures"
SCRIPTED_PROXY = TESTS_DIR / "scripted_proxy.py"


@pytest.fixture
def scripted_config():
    """A factory for an `AgentConfig` that runs `scripted_proxy.py` on one fixture.

    Call it with a fixture file name; any other `AgentConfig` field can be
    passed as a keyword, and `extra_args` is appended after the `--script` the
    proxy needs.
    """

    def make(fixture: str, *, extra_args: list[str] | None = None, **fields) -> AgentConfig:
        script = FIXTURES_DIR / fixture
        assert script.exists(), f"no such fixture: {script}"
        return AgentConfig(
            proxy_bin=str(SCRIPTED_PROXY),
            extra_args=["--script", str(script), *(extra_args or [])],
            **fields,
        )

    return make


@pytest.fixture
async def start_scripted(scripted_config):
    """Start a scripted `AgentProxy` and guarantee it is closed afterwards.

    Yields a coroutine function taking the same arguments as `scripted_config`.
    Every proxy it starts is closed at teardown, so a test that fails partway
    through still leaves no subprocess behind.
    """
    started = []

    async def start(fixture: str, **kwargs) -> AgentProxy:
        proxy = await AgentProxy.start(scripted_config(fixture, **kwargs))
        started.append(proxy)
        return proxy

    yield start
    for proxy in started:
        await proxy.close()


@pytest.fixture
async def fake_session():
    """A factory for a `Session` whose agent-proxy is a `FakeProxy`.

    Call it to get `(session, spawner)`. The session has not started its
    process yet — the first `submit` does that, and `spawner.latest` is the
    proxy it got. Pass a `spawner` to control what successive spawns return,
    or any `Session` keyword to override the defaults (a `MemoryTranscriptStore`
    and no `build_prompt`).

    Every session it builds is closed at teardown, so a test that fails
    partway leaves no read pump running.
    """
    built = []

    def make(
        topic: str = "topic-1",
        name: str = "s1",
        *,
        spawner: FakeSpawner | None = None,
        **fields,
    ) -> tuple[Session, FakeSpawner]:
        spawner = spawner or FakeSpawner()
        fields.setdefault("store", MemoryTranscriptStore())
        session = Session(topic, name, spawn=spawner, **fields)
        built.append(session)
        return session, spawner

    yield make
    for session in built:
        await session.close()


@pytest.fixture
async def started_session(fake_session):
    """A `Session` with its first turn already acknowledged.

    Yields `(session, proxy, turn_id)` for the many tests that care about what
    happens *during* a turn rather than about how one starts. `proxy` is the
    live `FakeProxy`, ready for `emit`.
    """

    async def start(**kwargs) -> tuple[Session, FakeProxy, str]:
        session, spawner = fake_session(**kwargs)
        turn_id = await session.submit("what host is this?")
        return session, spawner.latest, turn_id

    return start
