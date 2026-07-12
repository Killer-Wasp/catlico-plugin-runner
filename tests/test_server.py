"""Runner private HTTP server: health, plugins, and signed event push."""
import json

from starlette.testclient import TestClient

from plugin_runner.installer import STATE_FAILED, STATE_INSTALLED, InstallResult
from plugin_runner.registry import InstalledPlugin, Registry
from plugin_runner.sandbox import (
    ContainerSandboxRunner,
    SandboxRunResult,
    SubprocessSandboxRunner,
)
from plugin_runner.server import create_app, sign_body

PUSH_SECRET = "push-secret"


class _FakeClient:
    push_signing_secret = PUSH_SECRET

    async def claim_run(self, body):
        return {"outcome": "created", "run_id": "r1", "runtime_token": "t"}

    async def accept_run(self, run_id): ...
    async def get_run_config(self, run_id):
        return {"settings": {}, "secrets": {}}
    async def start_run(self, run_id): ...
    async def submit_result(self, run_id, body):
        self.submitted = body
    async def skip_run(self, run_id, reason): ...


class _FakeSandbox:
    async def run(self, request):
        return SandboxRunResult(run_id=request.run_id, status="success")


def _registry() -> Registry:
    r = Registry()
    r.add(
        InstalledPlugin(
            id="acme", version="1.0.0",
            manifest={"triggers": ["observable.created"]},
            module="acme.plugin", cls="Plugin", path="/p",
        )
    )
    return r


def _app():
    return create_app(
        client=_FakeClient(),
        registry=_registry(),
        runner_id="runner-1",
        api_base_url="http://catlico:8000",
        sandbox=_FakeSandbox(),
    )


def _app_with_sandbox(sandbox):
    return create_app(
        client=_FakeClient(),
        registry=_registry(),
        runner_id="runner-1",
        api_base_url="http://catlico:8000",
        sandbox=sandbox,
    )


def test_health():
    client = TestClient(_app())
    r = client.get("/internal/health")
    assert r.status_code == 200
    assert r.json()["installed_plugin_count"] == 1


def test_health_reports_subprocess_mode():
    client = TestClient(_app_with_sandbox(SubprocessSandboxRunner()))
    r = client.get("/internal/health")
    assert r.json()["isolation_mode"] == "subprocess"


def test_health_reports_container_mode():
    client = TestClient(_app_with_sandbox(ContainerSandboxRunner()))
    r = client.get("/internal/health")
    assert r.json()["isolation_mode"] == "container"


def test_health_defaults_to_subprocess_when_unset():
    # create_app defaults to the subprocess adapter when none is passed.
    client = TestClient(
        create_app(
            client=_FakeClient(),
            registry=_registry(),
            runner_id="runner-1",
            api_base_url="http://catlico:8000",
        )
    )
    r = client.get("/internal/health")
    assert r.json()["isolation_mode"] == "subprocess"


def test_plugins_lists_manifests():
    client = TestClient(_app())
    r = client.get("/internal/plugins")
    assert r.status_code == 200
    assert r.json()["plugins"][0]["id"] == "acme"


def test_event_requires_valid_signature():
    client = TestClient(_app())
    body = json.dumps({"event_type": "observable.created", "object": {}}).encode()

    unsigned = client.post("/internal/events", content=body)
    assert unsigned.status_code == 401

    bad = client.post(
        "/internal/events", content=body, headers={"x-catlico-signature": "sha256=deadbeef"}
    )
    assert bad.status_code == 401


def test_signed_event_dispatches():
    client = TestClient(_app())
    envelope = {
        "event_id": "audit:1",
        "event_type": "observable.created",
        "organisation_id": "org-a",
        "object": {"type": "observable", "id": "obs-1"},
    }
    body = json.dumps(envelope).encode()
    r = client.post(
        "/internal/events",
        content=body,
        headers={"x-catlico-signature": sign_body(body, PUSH_SECRET)},
    )
    assert r.status_code == 200, r.text
    assert r.json()["outcomes"]["acme"] == "success"


# --- install endpoint -------------------------------------------------------

class _CaptureClient:
    push_signing_secret = PUSH_SECRET

    def __init__(self):
        self.reports = []

    async def report_install_status(
        self, plugin_version_id, state, *, commit_sha=None,
        image_digest=None, install_log=None, error=None,
    ):
        self.reports.append(
            {
                "plugin_version_id": plugin_version_id,
                "state": state,
                "commit_sha": commit_sha,
                "image_digest": image_digest,
                "install_log": install_log,
                "error": error,
            }
        )


def _fake_installer(states, result):
    async def fake(source_url, source_ref, target_dir, *, on_state=None, **kwargs):
        for state in states:
            if on_state is not None:
                await on_state(state)
        return result

    return fake


def _install_app(cap, spawned, installer):
    return create_app(
        client=cap,
        registry=_registry(),
        runner_id="runner-1",
        api_base_url="http://catlico:8000",
        sandbox=_FakeSandbox(),
        installer=installer,
        spawn=lambda coro: spawned.append(coro),
    )


_INSTALL_BODY = json.dumps(
    {
        "plugin_version_id": "acme@1.0.0",
        "plugin_id": "acme",
        "source_url": "https://example.test/acme.git",
        "source_ref": "main",
    }
).encode()


def test_install_requires_valid_signature():
    cap = _CaptureClient()
    spawned = []
    client = TestClient(_install_app(cap, spawned, _fake_installer([], None)))

    unsigned = client.post("/internal/plugins/install", content=_INSTALL_BODY)
    assert unsigned.status_code == 401

    bad = client.post(
        "/internal/plugins/install",
        content=_INSTALL_BODY,
        headers={"x-catlico-signature": "sha256=deadbeef"},
    )
    assert bad.status_code == 401
    # A rejected request never spawns an install.
    assert spawned == []


async def test_signed_install_drives_state_sequence():
    cap = _CaptureClient()
    spawned = []
    result = InstallResult(
        status=STATE_INSTALLED,
        commit_sha="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        image_tag="catlico-plugin/acme:1.0.0",
    )
    installer = _fake_installer(
        ["cloning", "validating", "building", "installed"], result
    )
    client = TestClient(_install_app(cap, spawned, installer))

    r = client.post(
        "/internal/plugins/install",
        content=_INSTALL_BODY,
        headers={"x-catlico-signature": sign_body(_INSTALL_BODY, PUSH_SECRET)},
    )
    assert r.status_code == 202, r.text
    # Returned immediately; the install runs in the (captured) background task.
    assert len(spawned) == 1
    assert cap.reports == []

    await spawned[0]

    states = [rep["state"] for rep in cap.reports]
    assert states == ["cloning", "validating", "building", "installed"]
    terminal = cap.reports[-1]
    assert terminal["commit_sha"] == "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    assert terminal["image_digest"] == "catlico-plugin/acme:1.0.0"


async def test_install_reports_failed_when_installer_returns_failed():
    cap = _CaptureClient()
    spawned = []
    result = InstallResult(
        status=STATE_FAILED, errors=["image build failed"], log="boom log"
    )
    installer = _fake_installer(["cloning", "validating", "building"], result)
    client = TestClient(_install_app(cap, spawned, installer))

    r = client.post(
        "/internal/plugins/install",
        content=_INSTALL_BODY,
        headers={"x-catlico-signature": sign_body(_INSTALL_BODY, PUSH_SECRET)},
    )
    assert r.status_code == 202
    await spawned[0]

    terminal = cap.reports[-1]
    assert terminal["state"] == "failed"
    assert terminal["error"] == "image build failed"
    assert terminal["install_log"] == "boom log"


async def test_install_reports_failed_when_installer_raises():
    cap = _CaptureClient()
    spawned = []

    async def boom(*args, on_state=None, **kwargs):
        raise RuntimeError("clone exploded")

    client = TestClient(_install_app(cap, spawned, boom))
    r = client.post(
        "/internal/plugins/install",
        content=_INSTALL_BODY,
        headers={"x-catlico-signature": sign_body(_INSTALL_BODY, PUSH_SECRET)},
    )
    assert r.status_code == 202
    await spawned[0]

    assert cap.reports[-1]["state"] == "failed"
    assert "clone exploded" in cap.reports[-1]["error"]


def test_metrics_endpoint_exposes_prometheus_text():
    client = TestClient(_app())
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "plugin_runner_runs_claimed_total" in body
    assert "plugin_runner_runs_terminal_total" in body
    assert "plugin_runner_sandbox_run_duration_seconds" in body
