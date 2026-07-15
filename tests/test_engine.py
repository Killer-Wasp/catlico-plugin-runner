"""Event dispatch engine: claim → execute → report, loop suppression, quarantine."""
from plugin_runner.engine import dispatch_event
from plugin_runner.executor import RunResult
from plugin_runner.registry import STATUS_FAILED, InstalledPlugin, Registry


class FakeClient:
    """Records the lifecycle calls the engine makes."""

    def __init__(self, claim_outcome="created"):
        self._claim_outcome = claim_outcome
        self.calls: list[str] = []

    async def claim_run(self, body):
        self.calls.append("claim")
        if self._claim_outcome == "created":
            return {"outcome": "created", "run_id": "r1", "runtime_token": "tok"}
        return {"outcome": self._claim_outcome}

    async def accept_run(self, run_id):
        self.calls.append("accept")

    async def get_run_config(self, run_id):
        self.calls.append("config")
        return {"settings": {"threshold": 5}, "secrets": {"api_key": "k"}}

    async def start_run(self, run_id):
        self.calls.append("start")

    async def skip_run(self, run_id, reason):
        self.calls.append(f"skip:{reason}")

    async def submit_result(self, run_id, body):
        self.calls.append(f"result:{body['status']}")
        self.submitted = body


class FakeExecutor:
    def __init__(self, result):
        self._result = result
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        return self._result


def _plugin(**overrides) -> InstalledPlugin:
    base = dict(
        id="acme", version="1.0.0",
        manifest={"triggers": ["observable.created"], "permissions": ["read:observable"], "timeout_seconds": 30},
        module="main", app_object="catlico", path="/plugins/acme",
        venv_python="/venvs/acme-abc/bin/python",
    )
    base.update(overrides)
    return InstalledPlugin(**base)


def _registry() -> Registry:
    r = Registry()
    r.add(_plugin())
    return r


_ENVELOPE = {
    "event_id": "audit:1",
    "event_type": "observable.created",
    "organisation_id": "org-a",
    "object": {"type": "observable", "id": "obs-1"},
}


async def test_successful_dispatch_runs_full_lifecycle():
    client = FakeClient("created")
    executor = FakeExecutor(RunResult(run_id="r1", status="success", log_tail="ok"))
    summary = await dispatch_event(
        _ENVELOPE, client, _registry(), executor,
        runner_id="runner-1", api_base_url="http://catlico:8000",
    )
    assert summary["outcomes"]["acme"] == "success"
    assert client.calls == ["claim", "accept", "config", "start", "result:success"]
    req = executor.requests[0]
    assert req.run_token == "tok"
    assert req.secrets == {"api_key": "k"}
    assert req.plugin_module == "main"
    assert req.plugin_object == "catlico"
    assert req.python_executable == "/venvs/acme-abc/bin/python"
    assert req.declared_triggers == ["observable.created"]
    assert client.submitted["log_tail"] == "ok"


async def test_skip_reports_skip_not_result():
    client = FakeClient("created")
    executor = FakeExecutor(
        RunResult(run_id="r1", status="skipped", skip_reason="no matcher accepted")
    )
    await dispatch_event(
        _ENVELOPE, client, _registry(), executor,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert "skip:no matcher accepted" in client.calls
    assert not any(c.startswith("result:") for c in client.calls)


async def test_duplicate_claim_stops_before_execution():
    client = FakeClient("duplicate")
    executor = FakeExecutor(RunResult(run_id="r1", status="success"))
    summary = await dispatch_event(
        _ENVELOPE, client, _registry(), executor,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert summary["outcomes"]["acme"] == "duplicate"
    assert client.calls == ["claim"]
    assert executor.requests == []


async def test_plugin_actor_event_is_suppressed():
    client = FakeClient("created")
    executor = FakeExecutor(RunResult(run_id="r1", status="success"))
    summary = await dispatch_event(
        {**_ENVELOPE, "actor": "plugin:acme@1.0.0"}, client, _registry(), executor,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert summary["suppressed"] is True
    assert client.calls == []


# --- Quarantine ---


async def test_quarantined_plugin_is_clean_noop_never_claims():
    client = FakeClient("created")
    executor = FakeExecutor(RunResult(run_id="r1", status="success"))
    registry = Registry()
    registry.add(_plugin(status=STATUS_FAILED, error="venv sync failed"))
    summary = await dispatch_event(
        _ENVELOPE, client, registry, executor,
        runner_id="runner-1", api_base_url="http://c",
    )
    # for_trigger already filters quarantined, so this event never selects it.
    assert summary["outcomes"] == {}
    assert client.calls == []


async def test_targeted_quarantined_plugin_reports_quarantined():
    """A manual (targeted) run selects the plugin by id, bypassing for_trigger —
    so the engine itself must refuse a non-ready plugin and never claim."""
    client = FakeClient("created")
    executor = FakeExecutor(RunResult(run_id="r1", status="success"))
    registry = Registry()
    registry.add(_plugin(status=STATUS_FAILED, error="bad manifest"))
    envelope = {**_ENVELOPE, "target_plugin_id": "acme"}
    summary = await dispatch_event(
        envelope, client, registry, executor,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert summary["outcomes"] == {"acme": "quarantined"}
    assert client.calls == []
    assert executor.requests == []


# --- Targeted (manual) dispatch ---


def _second_plugin() -> InstalledPlugin:
    return _plugin(
        id="other",
        manifest={"triggers": ["case.created"], "permissions": [], "timeout_seconds": 30},
        module="main", app_object="catlico", path="/plugins/other",
    )


async def test_target_plugin_id_runs_only_that_plugin_bypassing_triggers():
    client = FakeClient("created")
    executor = FakeExecutor(RunResult(run_id="r1", status="success"))
    registry = _registry()
    registry.add(_second_plugin())
    envelope = {
        **_ENVELOPE,
        "event_type": "observable.manual",  # NOT in acme's triggers
        "target_plugin_id": "acme",
    }
    summary = await dispatch_event(
        envelope, client, registry, executor,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert summary["outcomes"] == {"acme": "success"}
    assert client.calls == ["claim", "accept", "config", "start", "result:success"]


async def test_unknown_target_plugin_is_clean_noop():
    client = FakeClient("created")
    executor = FakeExecutor(RunResult(run_id="r1", status="success"))
    envelope = {**_ENVELOPE, "target_plugin_id": "does-not-exist"}
    summary = await dispatch_event(
        envelope, client, _registry(), executor,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert summary == {"dispatched": 0, "suppressed": False, "outcomes": {}}
    assert client.calls == []
    assert executor.requests == []


async def test_absent_target_keeps_trigger_fanout():
    client = FakeClient("created")
    executor = FakeExecutor(RunResult(run_id="r1", status="success"))
    registry = _registry()
    registry.add(_second_plugin())  # triggers on case.created, must NOT run
    summary = await dispatch_event(
        _ENVELOPE, client, registry, executor,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert set(summary["outcomes"]) == {"acme"}
