"""Prometheus instrumentation for the plugin runner.

A dedicated, private ``CollectorRegistry`` — not the global default registry
used by ``prometheus_client``'s module-level shortcuts — so metrics stay
testable (each test module can construct its own registry without stepping on
another test's counters) and importing this module never mutates process-wide
state.

Label cardinality is intentionally bounded to small fixed enums (outcome,
status, error_kind, result). Never label by plugin_id, event_id, or run_id —
those are unbounded and would blow up Prometheus's memory/series count.

Call sites should use the ``record_*`` helpers below rather than reaching into
the metric objects directly, so instrumentation stays a one-line addition at
each call site.
"""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

#: Private registry for all runner metrics. Passed explicitly to
#: ``generate_latest`` by the ``/metrics`` route — never registered against
#: prometheus_client's global default REGISTRY.
REGISTRY = CollectorRegistry()

# --- Run lifecycle -----------------------------------------------------

# Note: a "skip" can surface at two distinct pipeline stages, so total skips =
# runs_claimed_total{outcome=skipped} (the API declined the claim up front)
# + runs_terminal_total{status=skipped} (the plugin ran and chose to skip).
# They are different events — do not treat either alone as "all skips".
runs_claimed_total = Counter(
    "plugin_runner_runs_claimed_total",
    "Runs claimed from the Catlico API, by claim outcome.",
    ["outcome"],  # created | duplicate | deferred | skipped
    registry=REGISTRY,
)

runs_terminal_total = Counter(
    "plugin_runner_runs_terminal_total",
    "Runs that reached a terminal status.",
    ["status"],  # success | failure | timeout | skipped
    registry=REGISTRY,
)

#: The only error_kind label values ever emitted. Anything else — including a
#: falsy/missing kind — is bucketed as "other" by ``record_run_failure``.
#: error_kind ultimately originates from untrusted plugin code (a plugin-defined
#: exception's ``error_kind`` attribute, read out of the sandbox's JSON), so it
#: MUST be clamped to this fixed set before becoming a Prometheus label —
#: otherwise a plugin could emit per-run-unique kinds and blow up label
#: cardinality.
ERROR_KINDS = frozenset({"transient", "config", "input", "bug"})

run_failures_total = Counter(
    "plugin_runner_run_failures_total",
    "Run failures, by error kind (clamped to a fixed enum; see ERROR_KINDS).",
    ["error_kind"],  # transient | config | input | bug | other
    registry=REGISTRY,
)

dispatch_errors_total = Counter(
    "plugin_runner_dispatch_errors_total",
    "Unhandled exceptions raised while dispatching a single plugin run "
    "(caught by the per-plugin fallback so one plugin cannot sink the event).",
    registry=REGISTRY,
)

suppressed_events_total = Counter(
    "plugin_runner_suppressed_events_total",
    "Events suppressed by loop-prevention (actor was a plugin).",
    registry=REGISTRY,
)

# --- Heartbeat -----------------------------------------------------------

heartbeats_total = Counter(
    "plugin_runner_heartbeats_total",
    "Heartbeat calls to the Catlico API, by result.",
    ["result"],  # success | failure
    registry=REGISTRY,
)

# --- Sandbox ---------------------------------------------------------------

sandbox_run_duration_seconds = Histogram(
    "plugin_runner_sandbox_run_duration_seconds",
    "Wall-clock time spent executing a plugin inside the sandbox.",
    registry=REGISTRY,
)

# --- Gauges ------------------------------------------------------------
# Both gauges reflect boot-time configuration: main.serve sets them once at
# startup (they don't change while the runner is up), so they read as static
# facts about this runner rather than live time-series.

installed_plugin_count = Gauge(
    "plugin_runner_installed_plugin_count",
    "Number of plugins currently installed/discovered by this runner.",
    registry=REGISTRY,
)

quarantined_plugin_count = Gauge(
    "plugin_runner_quarantined_plugin_count",
    "Number of discovered plugins quarantined (bad manifest or failed venv sync) "
    "and therefore not dispatchable.",
    registry=REGISTRY,
)

isolation_mode_info = Gauge(
    "plugin_runner_isolation_mode_info",
    "Always 1; the active isolation mode is carried on the `mode` label "
    "(standard Prometheus 'info gauge' pattern).",
    ["mode"],
    registry=REGISTRY,
)


# --- Record helpers ------------------------------------------------------


def record_claim_outcome(outcome: str) -> None:
    runs_claimed_total.labels(outcome=outcome).inc()


def record_terminal_status(status: str) -> None:
    runs_terminal_total.labels(status=status).inc()


def record_run_failure(error_kind: str | None) -> None:
    # Clamp to the fixed enum (untrusted origin — see ERROR_KINDS) and ALWAYS
    # record: a falsy/unknown kind buckets to "other" rather than being dropped,
    # so run_failures_total never silently diverges from
    # runs_terminal_total{status=failure|timeout} — every failure/timeout is
    # counted exactly once here.
    bucket = error_kind if error_kind in ERROR_KINDS else "other"
    run_failures_total.labels(error_kind=bucket).inc()


def record_dispatch_error() -> None:
    dispatch_errors_total.inc()


def record_suppressed_event() -> None:
    suppressed_events_total.inc()


def record_heartbeat(result: str) -> None:
    heartbeats_total.labels(result=result).inc()


def observe_sandbox_duration(seconds: float) -> None:
    sandbox_run_duration_seconds.observe(seconds)


def set_installed_plugin_count(count: int) -> None:
    installed_plugin_count.set(count)


def set_quarantined_plugin_count(count: int) -> None:
    quarantined_plugin_count.set(count)


def set_isolation_mode(mode: str) -> None:
    isolation_mode_info.labels(mode=mode).set(1)
