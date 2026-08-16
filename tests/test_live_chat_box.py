"""The chat box itself, against a real server and a real agent.

The last stand-in in the front-end's tests is the DOM, and this is where even
the socket stops being one: `tests/js/live_turn.js` mounts the shipped
`r2d2box.js` on a real WebSocket to a real uvicorn, which spawns a real
agent-proxy. What it proves that `tests/js/chat_box.test.js` cannot is that the
box's protocol assumptions match the server that implements them — the attach
handshake, the session it is given back, the `turn_prompt` it draws the
question from, and the `turn_end` it frees the composer on.

Doubly opt-in like the rest of the live tier:

    R2D2BOX_RUN_LIVE=1 pytest -m live
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from test_live_proxy import require_agent_proxy  # noqa: F401  (a fixture, used by name)

pytestmark = pytest.mark.live

TESTS_DIR = Path(__file__).parent
LIVE_TURN_JS = TESTS_DIR / "js" / "live_turn.js"
SERVER_START_TIMEOUT_S = 30
TURN_TIMEOUT_S = 240


def free_port() -> int:
    """A port nothing is listening on, for the server this test starts."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture
def live_server(require_agent_proxy, tmp_path):  # noqa: F811  (the imported fixture)
    """A real uvicorn serving `tests/live_app.py`, yielded as its base URL.

    Started as a subprocess rather than in-process because the point is a real
    socket: an ASGI client in the test's own loop is what the rest of the suite
    already uses, and cannot be reached from `node`.
    """
    if shutil.which("node") is None:
        pytest.skip("the chat box's live test needs node")
    workdir = tmp_path / "agent-cwd"
    workdir.mkdir()

    port = free_port()
    environment = dict(
        os.environ,
        R2D2BOX_LIVE_CWD=str(workdir),
        R2D2BOX_LIVE_STORE=str(tmp_path / "transcripts"),
        PYTHONPATH=os.pathsep.join([str(TESTS_DIR), os.environ.get("PYTHONPATH", "")]),
    )
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "live_app:app", "--port", str(port)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_until_serving(f"http://127.0.0.1:{port}/chat/sessions/probe", server)
        yield f"127.0.0.1:{port}"
    finally:
        server.terminate()
        try:
            server.wait(timeout=20)
        except subprocess.TimeoutExpired:
            server.kill()


def wait_until_serving(url: str, server: subprocess.Popen) -> None:
    """Block until the server answers, or fail with whatever it printed instead."""
    deadline = time.monotonic() + SERVER_START_TIMEOUT_S
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise AssertionError(f"the server exited early:\n{server.stdout.read()}")
        try:
            urllib.request.urlopen(url, timeout=1).read()
            return
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.2)
    raise AssertionError(f"the server never answered {url}")


def test_the_box_runs_a_real_turn_over_a_real_socket(live_server):
    """One question, asked and answered through the shipped front-end."""
    prompt = "Reply with exactly the single word PONG and nothing else."
    result = subprocess.run(
        ["node", str(LIVE_TURN_JS), f"ws://{live_server}/chat", "live-topic", prompt],
        capture_output=True,
        text=True,
        timeout=TURN_TIMEOUT_S + 60,
        env=dict(os.environ, R2D2BOX_LIVE_TIMEOUT_MS=str(TURN_TIMEOUT_S * 1000)),
        check=False,
    )
    assert result.returncode == 0, f"\n{result.stdout}\n{result.stderr}"
    drawn = json.loads(result.stdout.strip().splitlines()[-1])

    assert drawn["session"], "the box asked for a session and was given one"
    assert drawn["questions"] == [prompt], "it drew the question from `turn_prompt`"
    assert "PONG" in " ".join(drawn["answers"]).upper()
    assert drawn["composerEnabled"], "the turn ended, so the composer is usable again"
    assert drawn["notes"] == [], "nothing went wrong worth telling the reader about"
