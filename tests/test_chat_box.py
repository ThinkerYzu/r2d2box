"""The front-end's own suite, run from this one.

`tests/js/run.js` drives the shipped `r2d2box.js` against `tests/js/minidom.js`
— the browser layer's stand-in, in the same family as `scripted_proxy.py` and
`fake_proxy.py`. It needs `node` and nothing else: no browser, no jsdom, no
network, no package manager.

Running it from pytest rather than beside it means one command covers the whole
library, and a front-end regression fails the same suite as a server one. With
no `node` on the machine the test skips, so the default suite still passes with
nothing installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

JS_RUNNER = Path(__file__).parent / "js" / "run.js"


@pytest.mark.skipif(shutil.which("node") is None, reason="the chat box's tests need node")
def test_the_chat_box_passes_its_own_suite():
    """One test, because the failures the runner prints are more use than 36 stubs."""
    result = subprocess.run(
        ["node", str(JS_RUNNER)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"\n{result.stdout}\n{result.stderr}"
    assert "0 failed" in result.stdout
