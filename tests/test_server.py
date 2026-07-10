"""Runner private HTTP server: health, plugins, and signed event push."""
import json

from starlette.testclient import TestClient

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
