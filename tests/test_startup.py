"""Startup: uv preflight, serve self-registration, venv-result folding, sync CLI."""
import pytest

from plugin_runner import main
from plugin_runner.registry import STATUS_FAILED, STATUS_READY, InstalledPlugin, Registry
from plugin_runner.settings import RunnerSettings
from plugin_runner.venvs import VenvResult


async def _up() -> bool:
    return True


async def _down() -> bool:
    return False


# --- uv preflight ---


async def test_verify_uv_raises_when_unavailable():
    with pytest.raises(main.UvUnavailable):
        await main._verify_uv(_down)


async def test_verify_uv_passes_when_available():
    await main._verify_uv(_up)  # no raise


# --- fold venv results ---


def _plugin(pid="acme", **kw) -> InstalledPlugin:
    base = dict(id=pid, version="1", manifest={}, module="main", app_object="catlico", path="/p")
    base.update(kw)
    return InstalledPlugin(**base)


def test_fold_attaches_python_and_quarantines_failures():
    registry = Registry()
    registry.add(_plugin("ok"))
    registry.add(_plugin("broken"))
    results = {
        "ok": VenvResult("ok", "/venvs/ok/bin/python", True),
        "broken": VenvResult("broken", "", False, error="uv sync failed"),
    }
    quarantined = main._fold_venv_results(registry, results)
    assert quarantined == 1
    assert registry.get("ok").status == STATUS_READY
    assert registry.get("ok").venv_python == "/venvs/ok/bin/python"
    assert registry.get("broken").status == STATUS_FAILED
    assert "uv sync failed" in registry.get("broken").error


# --- serve() ---


class _FakeClient:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.register_called = False
        _FakeClient.instances.append(self)

    async def register(self, body):
        self.register_called = True
        self.register_body = body
        return {"id": "runner-1", "status": "healthy"}

    async def heartbeat(self, body):
        pass


class _FakeServer:
    def __init__(self, config):
        pass

    async def serve(self):
        return None


@pytest.fixture
def _stub_serve(monkeypatch):
    _FakeClient.instances = []
    monkeypatch.setattr(main, "PluginRunnerClient", _FakeClient)
    monkeypatch.setattr(main, "discover", lambda plugins_dir: Registry())

    async def _no_ensure(plugins, **kwargs):
        return {}

    monkeypatch.setattr(main.venvs, "ensure_all", _no_ensure)
    monkeypatch.setattr(main.uvicorn, "Server", _FakeServer)
    return _FakeClient


async def test_serve_refuses_to_start_when_uv_unavailable(_stub_serve):
    settings = RunnerSettings(plugins_dir="/plugins", shared_secret="s")
    with pytest.raises(main.UvUnavailable):
        await main.serve(settings, uv_check=_down)
    assert all(not c.register_called for c in _stub_serve.instances)


async def test_serve_registers_with_subprocess_isolation_mode(_stub_serve):
    settings = RunnerSettings(plugins_dir="/plugins", shared_secret="s")
    await main.serve(settings, uv_check=_up)
    registered = [c for c in _stub_serve.instances if c.register_called]
    assert registered
    assert registered[0].register_body["isolation_mode"] == "subprocess"


# --- sync CLI ---


async def test_sync_command_returns_nonzero_on_failure(monkeypatch):
    registry = Registry()
    registry.add(_plugin("ok", status=STATUS_READY))
    registry.add(_plugin("bad", status=STATUS_FAILED, error="boom"))

    async def _provision(settings, *, collect_garbage):
        return registry

    async def _uv_ok():
        return True

    monkeypatch.setattr(main, "_provision", _provision)
    monkeypatch.setattr(main, "_uv_available", _uv_ok)
    code = await main.sync_command(RunnerSettings(plugins_dir="/plugins"))
    assert code == 1


async def test_sync_command_returns_zero_when_all_ready(monkeypatch):
    registry = Registry()
    registry.add(_plugin("ok", status=STATUS_READY))

    async def _provision(settings, *, collect_garbage):
        return registry

    async def _uv_ok():
        return True

    monkeypatch.setattr(main, "_provision", _provision)
    monkeypatch.setattr(main, "_uv_available", _uv_ok)
    code = await main.sync_command(RunnerSettings(plugins_dir="/plugins"))
    assert code == 0
