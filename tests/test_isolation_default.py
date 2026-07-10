"""Container isolation is the default; subprocess is an explicit opt-in.

Covers the default setting, adapter selection (incl. rejecting unknown modes),
and the startup container-runtime preflight. The runtime check is injected so
these tests never shell out to Docker.
"""
import pytest

from plugin_runner import main
from plugin_runner.registry import Registry
from plugin_runner.sandbox import ContainerSandboxRunner, SubprocessSandboxRunner
from plugin_runner.settings import RunnerSettings


# --- default -----------------------------------------------------------------

def test_default_isolation_mode_is_container():
    assert RunnerSettings().isolation_mode == "container"


# --- adapter selection -------------------------------------------------------

def test_select_sandbox_defaults_to_container():
    sandbox = main.select_sandbox(RunnerSettings())
    assert isinstance(sandbox, ContainerSandboxRunner)


def test_select_sandbox_subprocess_when_explicit():
    sandbox = main.select_sandbox(RunnerSettings(isolation_mode="subprocess"))
    assert isinstance(sandbox, SubprocessSandboxRunner)


def test_select_sandbox_rejects_unknown_mode():
    with pytest.raises(ValueError) as exc:
        main.select_sandbox(RunnerSettings(isolation_mode="contianer"))
    message = str(exc.value)
    assert "contianer" in message
    # The error must name the valid values rather than silently degrade.
    assert "container" in message
    assert "subprocess" in message


# --- startup runtime preflight ----------------------------------------------

async def _down(runtime):
    return False


async def _up(runtime):
    return True


async def test_verify_container_runtime_raises_when_unavailable():
    with pytest.raises(main.ContainerRuntimeUnavailable) as exc:
        await main._verify_container_runtime(
            ContainerSandboxRunner(runtime="docker"), _down
        )
    message = str(exc.value)
    assert "docker" in message
    # Actionable opt-out must be named.
    assert "subprocess" in message


async def test_verify_container_runtime_passes_when_available():
    # No raise == proceeds.
    await main._verify_container_runtime(
        ContainerSandboxRunner(runtime="docker"), _up
    )


async def test_verify_skips_subprocess_adapter_even_if_check_fails():
    # Subprocess mode never touches a container runtime, so a "down" check
    # must not block startup.
    await main._verify_container_runtime(SubprocessSandboxRunner(), _down)


# --- serve() startup behaviour ----------------------------------------------

class _FakeClient:
    instances: list = []

    def __init__(self, *args, **kwargs):
        self.enroll_called = False
        _FakeClient.instances.append(self)

    async def enroll(self, body):
        self.enroll_called = True

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
    monkeypatch.setattr(main, "discover", lambda dirs: Registry())
    monkeypatch.setattr(main.uvicorn, "Server", _FakeServer)
    return _FakeClient


async def test_serve_refuses_to_start_when_runtime_unavailable(_stub_serve):
    settings = RunnerSettings(isolation_mode="container", plugin_dirs=[])
    with pytest.raises(main.ContainerRuntimeUnavailable):
        await main.serve(settings, runtime_check=_down)
    # Refuse means we never enroll — the runner must not register as healthy
    # and then fail every claimed run.
    assert all(not c.enroll_called for c in _stub_serve.instances)


async def test_serve_proceeds_when_runtime_available(_stub_serve):
    settings = RunnerSettings(isolation_mode="container", plugin_dirs=[])
    await main.serve(settings, runtime_check=_up)
    assert any(c.enroll_called for c in _stub_serve.instances)
