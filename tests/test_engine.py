"""Event dispatch engine: claim → sandbox → report, and loop suppression."""
from plugin_runner.engine import dispatch_event
from plugin_runner.registry import InstalledPlugin, Registry
from plugin_runner.sandbox import SandboxRunResult


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


class FakeSandbox:
    def __init__(self, result):
        self._result = result
        self.requests = []

    async def run(self, request):
        self.requests.append(request)
        return self._result


def _plugin() -> InstalledPlugin:
    return InstalledPlugin(
        id="acme", version="1.0.0",
        manifest={"triggers": ["observable.created"], "permissions": ["read:observable"], "timeout_seconds": 30},
        module="acme.plugin", cls="Plugin", path="/plugins/acme/src",
    )


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
    sandbox = FakeSandbox(SandboxRunResult(run_id="r1", status="success", log_tail="ok"))
    summary = await dispatch_event(
        _ENVELOPE, client, _registry(), sandbox,
        runner_id="runner-1", api_base_url="http://catlico:8000",
    )
    assert summary["outcomes"]["acme"] == "success"
    assert client.calls == ["claim", "accept", "config", "start", "result:success"]
    # Sandbox received config, secrets, token, and plugin coordinates.
    req = sandbox.requests[0]
    assert req.run_token == "tok"
    assert req.secrets == {"api_key": "k"}
    assert req.plugin_module == "acme.plugin"
    assert client.submitted["log_tail"] == "ok"


async def test_skip_reports_skip_not_result():
    client = FakeClient("created")
    sandbox = FakeSandbox(
        SandboxRunResult(run_id="r1", status="skipped", skip_reason="should_process")
    )
    await dispatch_event(
        _ENVELOPE, client, _registry(), sandbox,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert "skip:should_process" in client.calls
    assert not any(c.startswith("result:") for c in client.calls)


async def test_duplicate_claim_stops_before_execution():
    client = FakeClient("duplicate")
    sandbox = FakeSandbox(SandboxRunResult(run_id="r1", status="success"))
    summary = await dispatch_event(
        _ENVELOPE, client, _registry(), sandbox,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert summary["outcomes"]["acme"] == "duplicate"
    assert client.calls == ["claim"]
    assert sandbox.requests == []


async def test_plugin_actor_event_is_suppressed():
    client = FakeClient("created")
    sandbox = FakeSandbox(SandboxRunResult(run_id="r1", status="success"))
    summary = await dispatch_event(
        {**_ENVELOPE, "actor": "plugin:acme@1.0.0"}, client, _registry(), sandbox,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert summary["suppressed"] is True
    assert client.calls == []


# --- Targeted (manual) dispatch ---


def _second_plugin() -> InstalledPlugin:
    return InstalledPlugin(
        id="other", version="1.0.0",
        manifest={"triggers": ["case.created"], "permissions": [], "timeout_seconds": 30},
        module="other.plugin", cls="Plugin", path="/plugins/other/src",
    )


async def test_target_plugin_id_runs_only_that_plugin_bypassing_triggers():
    """A manual run targets one plugin and runs it even when the plugin's
    declared triggers do not cover the event type."""
    client = FakeClient("created")
    sandbox = FakeSandbox(SandboxRunResult(run_id="r1", status="success"))
    registry = _registry()
    registry.add(_second_plugin())
    envelope = {
        **_ENVELOPE,
        "event_type": "observable.manual",  # NOT in acme's triggers
        "target_plugin_id": "acme",
    }
    summary = await dispatch_event(
        envelope, client, registry, sandbox,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert summary["outcomes"] == {"acme": "success"}
    assert client.calls == ["claim", "accept", "config", "start", "result:success"]


async def test_unknown_target_plugin_is_clean_noop():
    client = FakeClient("created")
    sandbox = FakeSandbox(SandboxRunResult(run_id="r1", status="success"))
    envelope = {**_ENVELOPE, "target_plugin_id": "does-not-exist"}
    summary = await dispatch_event(
        envelope, client, _registry(), sandbox,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert summary == {"dispatched": 0, "suppressed": False, "outcomes": {}}
    assert client.calls == []
    assert sandbox.requests == []


async def test_absent_target_keeps_trigger_fanout():
    """No target -> unchanged behaviour: only trigger matches run, not everything."""
    client = FakeClient("created")
    sandbox = FakeSandbox(SandboxRunResult(run_id="r1", status="success"))
    registry = _registry()
    registry.add(_second_plugin())  # triggers on case.created, must NOT run
    summary = await dispatch_event(
        _ENVELOPE, client, registry, sandbox,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert set(summary["outcomes"]) == {"acme"}


async def test_targeted_envelope_still_suppresses_plugin_actor():
    client = FakeClient("created")
    sandbox = FakeSandbox(SandboxRunResult(run_id="r1", status="success"))
    envelope = {**_ENVELOPE, "target_plugin_id": "acme", "actor": "plugin:acme@1.0.0"}
    summary = await dispatch_event(
        envelope, client, _registry(), sandbox,
        runner_id="runner-1", api_base_url="http://c",
    )
    assert summary["suppressed"] is True
    assert client.calls == []
