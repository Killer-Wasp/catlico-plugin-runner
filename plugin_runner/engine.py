"""Event dispatch: turn one Catlico event into plugin runs.

For each ready plugin that declares the event's trigger, the runner claims a run
from Catlico (the API arbitrates duplicates/concurrency/skips), then executes the
plugin in a subprocess bound to its venv and reports the outcome back. The runner
never touches the database; all state changes go through the internal API.
"""
from __future__ import annotations

import logging
import time

from plugin_runner.client import PluginRunnerClient
from plugin_runner.executor import PluginExecutor, RunRequest
from plugin_runner.metrics import (
    observe_sandbox_duration,
    record_claim_outcome,
    record_dispatch_error,
    record_run_failure,
    record_suppressed_event,
    record_terminal_status,
)
from plugin_runner.registry import STATUS_READY, InstalledPlugin, Registry

logger = logging.getLogger(__name__)


def _claim_body(plugin: InstalledPlugin, envelope: dict, runner_id: str) -> dict:
    return {
        "event_id": envelope.get("event_id", ""),
        "event_type": envelope.get("event_type", ""),
        "organisation_id": envelope.get("organisation_id", ""),
        "plugin_id": plugin.id,
        "plugin_version": plugin.version,
        "runner_id": runner_id,
        "event_object": envelope.get("object", {}),
        "trigger_metadata": envelope.get("trigger_metadata", {}),
    }


async def _run_one(
    plugin: InstalledPlugin,
    envelope: dict,
    client: PluginRunnerClient,
    executor: PluginExecutor,
    *,
    runner_id: str,
    api_base_url: str,
) -> str:
    """Claim, execute, and report a single plugin run. Returns its outcome.

    A targeted (manual) run of a plugin that isn't ``ready`` (bad manifest, failed
    venv sync) is a clean "quarantined" no-op — the runner never spawns a broken
    plugin, and never claims a run it can't perform."""
    if plugin.status != STATUS_READY:
        logger.warning(
            "skipping quarantined plugin %s (status=%s): %s",
            plugin.id, plugin.status, plugin.error,
        )
        return "quarantined"

    claim = await client.claim_run(_claim_body(plugin, envelope, runner_id))
    outcome = claim["outcome"]
    record_claim_outcome(outcome)
    if outcome != "created":
        # duplicate / deferred / skipped are all terminal for this runner.
        return outcome

    run_id = claim["run_id"]
    token = claim["runtime_token"]
    await client.accept_run(run_id)
    config = await client.get_run_config(run_id)
    await client.start_run(run_id)

    request = RunRequest(
        run_id=run_id,
        plugin_module=plugin.module,
        plugin_object=plugin.app_object,
        event=envelope,
        config=config.get("settings", {}),
        secrets=config.get("secrets", {}),
        plugin_id=plugin.id,
        plugin_version=plugin.version,
        permissions=plugin.permissions,
        declared_triggers=plugin.triggers,
        plugin_path=plugin.path,
        python_executable=plugin.venv_python,
        timeout_seconds=plugin.timeout_seconds,
        api_base_url=api_base_url,
        run_token=token,
    )
    start = time.monotonic()
    result = await executor.run(request)
    observe_sandbox_duration(time.monotonic() - start)

    record_terminal_status(result.status)
    if result.status in ("failure", "timeout"):
        record_run_failure(result.error_kind)

    if result.status == "skipped":
        await client.skip_run(run_id, result.skip_reason or "should_process")
        return "skipped"

    await client.submit_result(
        run_id,
        {
            "status": result.status,
            "error": result.error,
            "error_kind": result.error_kind,
            "result_summary": result.result_summary,
            "operation_count": result.operation_count,
            "log_tail": result.log_tail,
        },
    )
    return result.status


async def dispatch_event(
    envelope: dict,
    client: PluginRunnerClient,
    registry: Registry,
    executor: PluginExecutor,
    *,
    runner_id: str,
    api_base_url: str,
) -> dict:
    """Distribute one event to installed plugins.

    Normally the event fans out to every locally-installed plugin whose triggers
    match. A ``target_plugin_id`` (set by a manual run) overrides that: run only
    that one plugin and bypass trigger matching entirely — analyst intent wins,
    even when the plugin's declared triggers don't cover the entity event. An
    unknown target is a clean no-op.
    """
    actor = envelope.get("actor", "")
    if isinstance(actor, str) and actor.startswith("plugin:"):
        # Loop prevention (defense in depth; the API also suppresses these).
        record_suppressed_event()
        return {"dispatched": 0, "suppressed": True, "outcomes": {}}

    target_plugin_id = envelope.get("target_plugin_id")
    if target_plugin_id:
        targeted = registry.get(target_plugin_id)
        plugins = [targeted] if targeted is not None else []
    else:
        event_type = envelope.get("event_type", "")
        plugins = registry.for_trigger(event_type)

    outcomes: dict[str, str] = {}
    for plugin in plugins:
        try:
            outcomes[plugin.id] = await _run_one(
                plugin, envelope, client, executor,
                runner_id=runner_id, api_base_url=api_base_url,
            )
        except Exception as exc:  # noqa: BLE001 — one plugin must not sink the event
            logger.exception("dispatch failed for plugin %s", plugin.id)
            record_dispatch_error()
            outcomes[plugin.id] = f"error: {type(exc).__name__}"
    return {"dispatched": len(outcomes), "suppressed": False, "outcomes": outcomes}
