"""Verify an installed release outside the checkout, including a live HTTP server."""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.error
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args()
    import r2d2box
    from r2d2box.proxy import DEFAULT_PROXY_BIN

    assert r2d2box.__version__ == args.version
    assert "site-packages" in str(Path(r2d2box.__file__))
    print("Installed proxy:", importlib.metadata.version("thinkeryzu-agent-proxy"))
    proxy = shutil.which(DEFAULT_PROXY_BIN, path=sysconfig.get_path("scripts"))
    assert proxy, "Dependency executable missing from installed environment"
    help_text = subprocess.check_output([proxy, "--help"], text=True)
    assert "--user-input" in help_text and "--no-bypass-permissions" in help_text
    subprocess.run([sys.executable, "-m", "r2d2box", "--version"], check=True)
    scripts = Path(sysconfig.get_path("scripts"))
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="r2d2box-smoke-") as work:
        with tempfile.TemporaryFile() as log:
            process = subprocess.Popen(
                [str(scripts / "r2d2box"), "--port", str(port), "--cwd", work,
                 "--store", str(Path(work) / "transcripts")],
                cwd=work, stdout=log, stderr=log,
            )
            try:
                root = f"http://127.0.0.1:{port}"
                for attempt in range(100):
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError(log.read().decode())
                    try:
                        with urllib.request.urlopen(root, timeout=1) as response:
                            assert b"/chat/static/r2d2box.js" in response.read()
                        break
                    except urllib.error.URLError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Installed server did not start")
                for asset in ["r2d2box.js", "r2d2box.css", "vendor/marked.min.js", "vendor/purify.min.js"]:
                    with urllib.request.urlopen(root + "/chat/static/" + asset, timeout=5) as response:
                        assert response.status == 200 and response.read()
                with urllib.request.urlopen(root + "/chat/sessions/release-smoke", timeout=5) as response:
                    assert response.status == 200
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
    print(f"Installed r2d2box {args.version}: CLI, dependency, page, assets and REST passed")


if __name__ == "__main__":
    main()
