"""The standalone host uses real routes and storage without a real agent."""

import pytest

from asgi_client import request, websocket
from fake_proxy import FakeHost, wait_until
from r2d2box import AgentConfig
from r2d2box import cli


def test_default_launch(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    launches = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: launches.append((app, kw)))
    cli.main([])
    app, options = launches[0]
    assert options == dict(host="127.0.0.1", port=8790, ws="websockets", workers=1)
    assert (tmp_path / ".local/share/r2d2box/transcripts").is_dir()


async def test_configured_launch(monkeypatch, tmp_path):
    launches = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: launches.append((app, kw)))
    cli.main(["--host", "0.0.0.0", "--port", "8900", "--agent", "codex",
              "--cwd", str(tmp_path), "--store", str(tmp_path / "store"),
              "--no-bypass-permissions", "--append-system-prompt", "Be brief.",
              "--proxy-bin", "custom-proxy", "--proxy-arg=--model", "--proxy-arg=test"])
    app, options = launches[0]
    assert options["host"] == "0.0.0.0" and options["port"] == 8900
    captured = []

    async def start(config, resume, tag):
        captured.append(config)
        return await FakeHost().start_proxy(config, resume, tag)

    host = app.state.box.host
    monkeypatch.setattr(host, "start_proxy", start)
    async with app.router.lifespan_context(app):
        session = await host.session("topic", "s1")
        await session.submit("hello")
        config = captured[0]
        assert config == AgentConfig(cwd=tmp_path, agent="codex", bypass_permissions=False,
                                     append_system_prompt="Be brief.", proxy_bin="custom-proxy",
                                     extra_args=["--model", "test"], allowed_tools=[])


@pytest.mark.parametrize("args, message", [
    (["--port", "65536"], "--port must"),
    (["--cwd", "/does-not-exist-r2d2box"], "--cwd is not a directory"),
    (["--agent", "codex", "--allowed-tool", "Bash"], "no codex equivalent"),
    (["--proxy-arg=--resume"], "owns the agent's session id"),
])
def test_invalid_configuration(args, message, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("content", ["{bad json", "[]"])
def test_bad_mcp_config(tmp_path, content):
    path = tmp_path / "mcp.json"
    path.write_text(content)
    with pytest.raises(SystemExit) as exc:
        cli.main(["--mcp-config", str(path)])
    assert exc.value.code == 2


async def test_sessions_assets_persistence_and_shutdown(tmp_path, monkeypatch):
    app = cli.create_app(AgentConfig(cwd=tmp_path), tmp_path)
    host = app.state.box.host
    fake = FakeHost()
    monkeypatch.setattr(host, "start_proxy", fake.start_proxy)
    async with app.router.lifespan_context(app):
        page = await request(app, "GET", "/")
        assert page.status == 200
        assert b"R2D2Box.mount" in page.body
        for asset in ("r2d2box.js", "r2d2box.css", "vendor/marked.min.js", "vendor/purify.min.js"):
            assert (await request(app, "GET", f"/chat/static/{asset}")).status == 200
        created = await request(app, "POST", "/chat/sessions/topic")
        assert created.status == 201
        name = created.json()["session"]
        assert fake.spawns == []
        async with websocket(app, "/chat/ws") as ws:
            await ws.send_json(dict(type="attach", topic="topic", session=name))
            assert (await ws.receive_json())["type"] == "attached"
            await ws.send_json(dict(type="submit", text="hello"))
            await wait_until(lambda: bool(fake.proxies), what="standalone host spawn")
            proxy = fake.proxies[("topic", name)]
            await wait_until(lambda: bool(proxy.submits), what="prompt submission")
            await proxy.run_turn("t-1", text="hello back")
            await ws.receive_until("turn_end")
        listed = (await request(app, "GET", "/chat/sessions/topic")).json()["sessions"]
        assert listed[0]["turns"] == 1
    assert proxy.closed
    restored = cli.create_app(AgentConfig(cwd=tmp_path), tmp_path)
    async with restored.router.lifespan_context(restored):
        listed = (await request(restored, "GET", "/chat/sessions/topic")).json()["sessions"]
        assert listed[0]["session"] == name
        assert listed[0]["preview"] == "hello"
        assert (await request(restored, "DELETE", f"/chat/sessions/topic/{name}")).status == 200
        assert (await request(restored, "GET", "/chat/sessions/topic")).json()["sessions"] == []
