"""Prometheus instrumentation: dispatch_event increments the right counters
and observes execution duration on the runner's private registry.

Mirrors tests/test_engine.py's fakes rather than importing them, since test
modules here are standalone (no shared fixtures/__init__.py).
"""
from plugin_runner.engine import dispatch_event
from plugin_runner.executor import RunResult
from plugin_runner.metrics import REGISTRY, record_run_failure
from plugin_runner.registry import InstalledPlugin, Registry


class FakeClient:
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
        return {"settings": {}, "secrets": {}}

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


class BrokenExecutor:
    """Raises inside the plugin's run, exercising the dispatch-error fallback."""

    async def run(self, request):
        raise RuntimeError("boom")


def _plugin(plugin_id="acme") -> InstalledPlugin:
    return InstalledPlugin(
        id=plugin_id, version="1.0.0",
        manifest={"triggers": ["observable.created"], "permissions": [], "timeout_seconds": 30},
        module="main", app_object="catlico", path="/plugins/acme",
    )


def _registry(plugin_id="acme") -> Registry:
    r = Registry()
    r.add(_plugin(plugin_id))
    return r


_ENVELOPE = {
    "event_id": "audit:1",
    "event_type": "observable.created",
    "organisation_id": "org-a",
    "object": {"type": "observable", "id": "obs-1"},
}


def _counter_value(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _histogram_count(name: str) -> float:
    return REGISTRY.get_sample_value(f"{name}_count") or 0.0


async def test_created_success_run_increments_claim_terminal_and_duration():
    before_claimed = _counter_value("plugin_runner_runs_claimed_total", outcome="created")
    before_terminal = _counter_value("plugin_runner_runs_terminal_total", status="success")
    before_duration_count = _histogram_count("plugin_runner_sandbox_run_duration_seconds")

    client = FakeClient("created")
    executor = FakeExecutor(RunResult(run_id="r1", status="success"))
    summary = await dispatch_event(
        _ENVELOPE, client, _registry(), executor,
        runner_id="runner-1", api_base_url="http://c",
    )

    assert summary["outcomes"]["acme"] == "success"
    assert _counter_value("plugin_runner_runs_claimed_total", outcome="created") == before_claimed + 1
    assert _counter_value("plugin_runner_runs_terminal_total", status="success") == before_terminal + 1
    assert (
        _histogram_count("plugin_runner_sandbox_run_duration_seconds")
        == before_duration_count + 1
    )


async def test_suppressed_event_increments_suppressed_counter():
    before = _counter_value("plugin_runner_suppressed_events_total")

    client = FakeClient("created")
    executor = FakeExecutor(RunResult(run_id="r1", status="success"))
    summary = await dispatch_event(
        {**_ENVELOPE, "actor": "plugin:acme@1.0.0"}, client, _registry(), executor,
        runner_id="runner-1", api_base_url="http://c",
    )

    assert summary["suppressed"] is True
    assert _counter_value("plugin_runner_suppressed_events_total") == before + 1


async def test_failure_with_error_kind_increments_failure_counter():
    before_terminal = _counter_value("plugin_runner_runs_terminal_total", status="failure")
    before_failure = _counter_value("plugin_runner_run_failures_total", error_kind="transient")

    client = FakeClient("created")
    executor = FakeExecutor(
        RunResult(run_id="r1", status="failure", error="boom", error_kind="transient")
    )
    summary = await dispatch_event(
        _ENVELOPE, client, _registry(), executor,
        runner_id="runner-1", api_base_url="http://c",
    )

    assert summary["outcomes"]["acme"] == "failure"
    assert _counter_value("plugin_runner_runs_terminal_total", status="failure") == before_terminal + 1
    assert (
        _counter_value("plugin_runner_run_failures_total", error_kind="transient")
        == before_failure + 1
    )


def _error_kind_label_values() -> set[str]:
    """Every error_kind label value currently present on run_failures_total."""
    values = set()
    for metric in REGISTRY.collect():
        if metric.name == "plugin_runner_run_failures":
            for sample in metric.samples:
                if sample.name == "plugin_runner_run_failures_total":
                    values.add(sample.labels["error_kind"])
    return values


def test_bogus_error_kind_clamped_to_other_no_new_label():
    """An arbitrary (untrusted, plugin-supplied) error_kind must NOT become a
    new label value — it buckets into "other" — guarding label cardinality."""
    before = _counter_value("plugin_runner_run_failures_total", error_kind="other")
    bogus = "per-run-unique-9f3c2a-attacker-controlled"

    record_run_failure(bogus)

    labels = _error_kind_label_values()
    assert bogus not in labels
    assert labels <= {"transient", "config", "input", "bug", "other"}
    assert _counter_value("plugin_runner_run_failures_total", error_kind="other") == before + 1


def test_none_error_kind_buckets_to_other_never_dropped():
    """A falsy error_kind still counts as a failure (bucketed "other") so
    run_failures_total does not silently diverge from the terminal-status count."""
    before = _counter_value("plugin_runner_run_failures_total", error_kind="other")

    record_run_failure(None)
    record_run_failure("")

    assert (
        _counter_value("plugin_runner_run_failures_total", error_kind="other")
        == before + 2
    )
    assert "" not in _error_kind_label_values()


async def test_dispatch_exception_increments_dispatch_error_counter():
    before = _counter_value("plugin_runner_dispatch_errors_total")

    client = FakeClient("created")
    summary = await dispatch_event(
        _ENVELOPE, client, _registry(), BrokenExecutor(),
        runner_id="runner-1", api_base_url="http://c",
    )

    assert summary["outcomes"]["acme"].startswith("error:")
    assert _counter_value("plugin_runner_dispatch_errors_total") == before + 1
