"""Event dispatch: turn one Catlico event into plugin runs.

For each installed plugin that declares the event's trigger, the runner claims a
run from Catlico (the API arbitrates duplicates/concurrency/skips), then executes
the plugin in a sandbox and reports the outcome back. The runner never touches the
database; all state changes go through the internal API.
"""
from __future__ import annotations

import logging

from plugin_runner.client import PluginRunnerClient
from plugin_runner.registry import InstalledPlugin, Registry
from plugin_runner.sandbox import SandboxRunner, SandboxRunRequest

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
    sandbox: SandboxRunner,
    *,
    runner_id: str,
    api_base_url: str,
) -> str:
    """Claim, execute, and report a single plugin run. Returns its outcome."""
    claim = await client.claim_run(_claim_body(plugin, envelope, runner_id))
    outcome = claim["outcome"]
    if outcome != "created":
        # duplicate / deferred / skipped are all terminal for this runner.
        return outcome

    run_id = claim["run_id"]
    token = claim["runtime_token"]
    await client.accept_run(run_id)
    config = await client.get_run_config(run_id)
    await client.start_run(run_id)

    request = SandboxRunRequest(
        run_id=run_id,
        plugin_module=plugin.module,
        plugin_class=plugin.cls,
        event=envelope,
        config=config.get("settings", {}),
        secrets=config.get("secrets", {}),
        plugin_id=plugin.id,
        plugin_version=plugin.version,
        permissions=plugin.permissions,
        plugin_path=plugin.path,
        timeout_seconds=plugin.timeout_seconds,
        api_base_url=api_base_url,
        run_token=token,
    )
    result = await sandbox.run(request)

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
    sandbox: SandboxRunner,
    *,
    runner_id: str,
    api_base_url: str,
) -> dict:
    """Distribute one event to every locally-installed plugin that wants it."""
    actor = envelope.get("actor", "")
    if isinstance(actor, str) and actor.startswith("plugin:"):
        # Loop prevention (defense in depth; the API also suppresses these).
        return {"dispatched": 0, "suppressed": True, "outcomes": {}}

    event_type = envelope.get("event_type", "")
    outcomes: dict[str, str] = {}
    for plugin in registry.for_trigger(event_type):
        try:
            outcomes[plugin.id] = await _run_one(
                plugin, envelope, client, sandbox,
                runner_id=runner_id, api_base_url=api_base_url,
            )
        except Exception as exc:  # noqa: BLE001 — one plugin must not sink the event
            logger.exception("dispatch failed for plugin %s", plugin.id)
            outcomes[plugin.id] = f"error: {type(exc).__name__}"
    return {"dispatched": len(outcomes), "suppressed": False, "outcomes": outcomes}
