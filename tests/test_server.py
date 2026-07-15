"""Runner private HTTP server: health, plugins (with status), signed events, rescan."""
import json

from starlette.testclient import TestClient

from plugin_runner.executor import RunResult, SubprocessExecutor
from plugin_runner.registry import STATUS_FAILED, InstalledPlugin, Registry
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


class _FakeExecutor:
    isolation_mode = "subprocess"

    async def run(self, request):
        return RunResult(run_id=request.run_id, status="success")


def _plugin(**overrides) -> InstalledPlugin:
    base = dict(
        id="acme", version="1.0.0",
        manifest={"triggers": ["observable.created"]},
        module="main", app_object="catlico", path="/plugins/acme",
    )
    base.update(overrides)
    return InstalledPlugin(**base)


def _registry() -> Registry:
    r = Registry()
    r.add(_plugin())
    return r


def _app(**kwargs):
    return create_app(
        client=_FakeClient(),
        registry=kwargs.pop("registry", _registry()),
        runner_id="runner-1",
        api_base_url="http://catlico:8000",
        executor=kwargs.pop("executor", _FakeExecutor()),
        **kwargs,
    )


def test_health():
    client = TestClient(_app())
    r = client.get("/internal/health")
    assert r.status_code == 200
    assert r.json()["installed_plugin_count"] == 1
    assert r.json()["isolation_mode"] == "subprocess"


def test_health_defaults_to_subprocess_executor_when_unset():
    app = create_app(
        client=_FakeClient(),
        registry=_registry(),
        runner_id="runner-1",
        api_base_url="http://catlico:8000",
    )
    r = TestClient(app).get("/internal/health")
    assert r.json()["isolation_mode"] == "subprocess"


def test_plugins_lists_status_and_error():
    registry = Registry()
    registry.add(_plugin())
    registry.add(_plugin(id="broken", status=STATUS_FAILED, error="venv sync failed"))
    r = TestClient(_app(registry=registry)).get("/internal/plugins")
    assert r.status_code == 200
    by_id = {p["id"]: p for p in r.json()["plugins"]}
    assert by_id["acme"]["status"] == "ready"
    assert by_id["broken"]["status"] == "failed"
    assert by_id["broken"]["error"] == "venv sync failed"


def test_event_requires_valid_signature():
    client = TestClient(_app())
    body = json.dumps({"event_type": "observable.created", "object": {}}).encode()
    assert client.post("/internal/events", content=body).status_code == 401
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


# --- rescan (implemented-but-unwired) ---

_RESCAN_BODY = b"{}"


def test_rescan_requires_valid_signature():
    spawned = []
    app = _app(rescan=_noop_rescan, spawn=lambda coro: spawned.append(coro))
    client = TestClient(app)
    assert client.post("/internal/plugins/rescan", content=_RESCAN_BODY).status_code == 401
    assert spawned == []


async def _noop_rescan():
    _noop_rescan.called = True


async def test_signed_rescan_spawns_background_provision():
    calls = {"n": 0}

    async def rescan():
        calls["n"] += 1

    spawned = []
    app = _app(rescan=rescan, spawn=lambda coro: spawned.append(coro))
    client = TestClient(app)
    r = client.post(
        "/internal/plugins/rescan",
        content=_RESCAN_BODY,
        headers={"x-catlico-signature": sign_body(_RESCAN_BODY, PUSH_SECRET)},
    )
    assert r.status_code == 202
    assert len(spawned) == 1
    await spawned[0]
    assert calls["n"] == 1


def test_rescan_returns_501_when_not_configured():
    client = TestClient(_app())  # no rescan callable wired
    r = client.post(
        "/internal/plugins/rescan",
        content=_RESCAN_BODY,
        headers={"x-catlico-signature": sign_body(_RESCAN_BODY, PUSH_SECRET)},
    )
    assert r.status_code == 501


def test_metrics_endpoint_exposes_prometheus_text():
    client = TestClient(_app())
    r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "plugin_runner_runs_claimed_total" in body
    assert "plugin_runner_quarantined_plugin_count" in body


def test_real_subprocess_executor_reports_subprocess_mode():
    app = _app(executor=SubprocessExecutor())
    r = TestClient(app).get("/internal/health")
    assert r.json()["isolation_mode"] == "subprocess"
