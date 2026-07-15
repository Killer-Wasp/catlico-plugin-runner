"""Container isolation is the default; subprocess is an explicit opt-in.

Covers the default setting, adapter selection (incl. rejecting unknown modes),
and the startup container-runtime preflight. The runtime check is injected so
these tests never shell out to Docker.
"""
import pytest

from plugin_runner import main
from plugin_runner.registry import Registry
from plugin_runner.sandbox import (
    ContainerSandboxRunner,
    SandboxRunRequest,
    SubprocessSandboxRunner,
    build_container_command,
)
from plugin_runner.settings import RunnerSettings


# --- default -----------------------------------------------------------------

def test_default_isolation_mode_is_container():
    assert RunnerSettings().isolation_mode == "container"


def test_default_container_runtime_and_network():
    settings = RunnerSettings()
    assert settings.container_runtime == "docker"
    assert settings.container_network == "bridge"


def test_container_runtime_and_network_overridable_via_kwargs():
    settings = RunnerSettings(container_runtime="podman", container_network="none")
    assert settings.container_runtime == "podman"
    assert settings.container_network == "none"


def test_container_runtime_and_network_overridable_via_env(monkeypatch):
    monkeypatch.setenv("PLUGIN_RUNNER_CONTAINER_RUNTIME", "podman")
    monkeypatch.setenv("PLUGIN_RUNNER_CONTAINER_NETWORK", "none")
    settings = RunnerSettings()
    assert settings.container_runtime == "podman"
    assert settings.container_network == "none"


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


def test_select_sandbox_defaults_carry_docker_bridge():
    sandbox = main.select_sandbox(RunnerSettings())
    assert sandbox._runtime == "docker"
    assert sandbox._network == "bridge"


def test_select_sandbox_honours_configured_runtime_and_network():
    settings = RunnerSettings(container_runtime="podman", container_network="none")
    sandbox = main.select_sandbox(settings)
    assert isinstance(sandbox, ContainerSandboxRunner)
    assert sandbox._runtime == "podman"
    assert sandbox._network == "none"


def test_select_sandbox_wiring_reaches_build_container_command():
    """End-to-end: a configured runtime/network flows from settings through the
    adapter constructed by select_sandbox into the actual command argv."""
    settings = RunnerSettings(container_runtime="podman", container_network="none")
    sandbox = main.select_sandbox(settings)
    request = SandboxRunRequest(
        run_id="run-1", plugin_module="acme.plugin", plugin_class="Plugin",
        event={}, plugin_id="acme", plugin_version="1.0.0",
    )
    cmd = build_container_command(
        request,
        image=sandbox._image_for(request),
        runtime=sandbox._runtime,
        container_name="c1",
        network=sandbox._network,
    )
    assert cmd[0] == "podman"
    assert "--network none" in " ".join(cmd)


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
        self.register_called = False
        _FakeClient.instances.append(self)

    async def register(self, body):
        self.register_called = True
        return {"id": "runner-1", "status": "healthy"}

    async def sync(self):
        return {}

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


async def test_serve_refuses_to_start_when_runtime_unavailable(_stub_serve, tmp_path):
    # The container-runtime preflight must abort before the runner self-registers.
    settings = RunnerSettings(
        isolation_mode="container",
        plugin_dirs=[],
        shared_secret="test-secret",
    )
    with pytest.raises(main.ContainerRuntimeUnavailable):
        await main.serve(settings, runtime_check=_down)
    # Refuse means we never register — the runner must not register as healthy
    # and then fail every claimed run.
    assert all(not c.register_called for c in _stub_serve.instances)


async def test_serve_proceeds_when_runtime_available(_stub_serve, tmp_path):
    # A usable runtime lets serve() self-register on startup.
    settings = RunnerSettings(
        isolation_mode="container",
        plugin_dirs=[],
        shared_secret="test-secret",
    )
    await main.serve(settings, runtime_check=_up)
    assert any(c.register_called for c in _stub_serve.instances)
