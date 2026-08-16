"""Shared fixtures: pointing an `AgentConfig` at the scripted proxy.

Nothing here fakes `proxy.py`. The tests that use these fixtures run a real
subprocess over real pipes, which is what makes them able to catch the buffer
and shutdown behavior at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from r2d2box import AgentConfig, AgentProxy

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
