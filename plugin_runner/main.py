"""Runner entrypoint: enroll, start the heartbeat loop, serve the private API."""
from __future__ import annotations

import asyncio
import logging

import uvicorn

from plugin_runner.client import PluginRunnerClient
from plugin_runner.installer import STATE_INSTALLED, ensure_images
from plugin_runner.registry import discover
from plugin_runner.sandbox import ContainerSandboxRunner, SandboxRunner, SubprocessSandboxRunner
from plugin_runner.server import create_app
from plugin_runner.settings import RunnerSettings, load_settings

logger = logging.getLogger(__name__)


def select_sandbox(settings: RunnerSettings) -> SandboxRunner:
    """Container isolation is the default for untrusted plugins; the trusted
    subprocess adapter is opt-in via ``isolation_mode = subprocess``."""
    if settings.isolation_mode == "subprocess":
        return SubprocessSandboxRunner()
    return ContainerSandboxRunner()


async def _ensure_plugin_images(sandbox: SandboxRunner, registry) -> None:
    """Build any per-plugin container images the active adapter needs before we
    start serving. Keyed off the adapter itself (not ``settings.isolation_mode``)
    so this keeps working when the isolation default flips: only the container
    adapter runs plugins from per-plugin images, so the subprocess adapter has
    nothing to build. Broken plugins are logged and skipped, never fatal."""
    if not isinstance(sandbox, ContainerSandboxRunner):
        return
    runtime = getattr(sandbox, "_runtime", "docker")
    states = await ensure_images(registry.all(), runtime=runtime)
    unavailable = sorted(pid for pid, state in states.items() if state != STATE_INSTALLED)
    if unavailable:
        logger.warning(
            "plugin image(s) unavailable — runs for these will fail: %s",
            ", ".join(unavailable),
        )


def _register_body(settings: RunnerSettings, registry) -> dict:
    return {
        "id": settings.runner_id,
        "name": settings.name,
        "version": settings.version,
        "capabilities": ["enrichment"],
        "isolation_mode": settings.isolation_mode,
        "enrollment_token": settings.enrollment_token,
        "plugins": registry.manifests(),
    }


async def _heartbeat_loop(
    client: PluginRunnerClient, settings: RunnerSettings, registry
) -> None:
    while True:
        try:
            await client.heartbeat(
                {
                    "runner_id": settings.runner_id,
                    "capacity": 10,
                    "active_run_count": 0,
                    "installed_plugin_count": len(registry.all()),
                    "health_summary": "ok",
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — heartbeat must survive API blips
            logger.exception("heartbeat failed")
        await asyncio.sleep(settings.heartbeat_interval_seconds)


async def serve(settings: RunnerSettings | None = None) -> None:
    settings = settings or load_settings()
    registry = discover(settings.plugin_dirs)
    logger.info("discovered %d plugin(s)", len(registry.all()))

    sandbox = select_sandbox(settings)
    await _ensure_plugin_images(sandbox, registry)

    client = PluginRunnerClient(settings.catlico_api_url, timeout=settings.http_timeout)
    await client.enroll(_register_body(settings, registry))
    logger.info("enrolled runner %s", settings.runner_id)

    app = create_app(
        client=client,
        registry=registry,
        runner_id=settings.runner_id,
        api_base_url=settings.catlico_api_url,
        sandbox=sandbox,
    )
    heartbeat = asyncio.create_task(_heartbeat_loop(client, settings, registry))
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        heartbeat.cancel()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
