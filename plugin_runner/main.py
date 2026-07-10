"""Runner entrypoint: enroll, start the heartbeat loop, serve the private API."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx
import uvicorn

from plugin_runner.client import PluginRunnerClient
from plugin_runner.installer import STATE_INSTALLED, ensure_images
from plugin_runner.registry import discover
from plugin_runner.sandbox import ContainerSandboxRunner, SandboxRunner, SubprocessSandboxRunner
from plugin_runner.server import create_app
from plugin_runner.settings import RunnerSettings, load_settings
from plugin_runner.state import RunnerState, load_state, save_state

logger = logging.getLogger(__name__)

#: Isolation modes the runner knows how to select an adapter for.
VALID_ISOLATION_MODES = ("container", "subprocess")

#: Signature of the injectable container-runtime preflight: given the runtime
#: name (e.g. ``"docker"``), return whether it is actually usable.
RuntimeCheck = Callable[[str], Awaitable[bool]]


class ContainerRuntimeUnavailable(RuntimeError):
    """Raised at startup when container isolation is selected but the container
    runtime is not usable. Refusing to start is deliberate: see
    ``_verify_container_runtime``."""


def select_sandbox(settings: RunnerSettings) -> SandboxRunner:
    """Container isolation is the default for untrusted plugins; the trusted
    subprocess adapter is opt-in via ``isolation_mode = subprocess``.

    An unrecognised ``isolation_mode`` is a hard error — we never silently fall
    back to an adapter the operator did not ask for (a typo like ``contianer``
    could otherwise quietly land plugins in either isolation posture)."""
    if settings.isolation_mode == "subprocess":
        return SubprocessSandboxRunner()
    if settings.isolation_mode == "container":
        return ContainerSandboxRunner()
    raise ValueError(
        f"invalid isolation_mode {settings.isolation_mode!r}; "
        f"valid values are: {', '.join(VALID_ISOLATION_MODES)}"
    )


async def _container_runtime_available(runtime: str) -> bool:
    """Default preflight: is ``<runtime> version`` runnable and returning 0?

    Injected in tests (via ``serve(..., runtime_check=...)``) so the suite never
    shells out to Docker."""
    try:
        proc = await asyncio.create_subprocess_exec(
            runtime, "version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return False
    try:
        return (await asyncio.wait_for(proc.wait(), timeout=10)) == 0
    except asyncio.TimeoutError:
        proc.kill()
        return False


async def _verify_container_runtime(
    sandbox: SandboxRunner, runtime_check: RuntimeCheck
) -> None:
    """Fail loudly and early if container isolation is selected but its runtime
    is unusable.

    We refuse to start rather than start degraded. A runner that starts without
    a working runtime would still answer ``/internal/health`` as healthy, keep
    heartbeating, and claim runs — then fail every single one late, per-run.
    Silently accepting work it cannot perform is worse than not starting, so the
    process exits and an operator sees the failure immediately.

    No-op for the subprocess adapter, which never touches a container runtime."""
    if not isinstance(sandbox, ContainerSandboxRunner):
        return
    runtime = getattr(sandbox, "_runtime", "docker")
    if await runtime_check(runtime):
        return
    logger.error(
        "container isolation is enabled but the container runtime %r is not "
        "usable (a `%s version` preflight failed). Every plugin run would fail. "
        "Refusing to start. Fix the runtime (is %r installed, on PATH, and its "
        "daemon running?), or explicitly opt out by setting "
        "PLUGIN_RUNNER_ISOLATION_MODE=subprocess — which DISABLES all isolation "
        "(no container, read-only rootfs, resource caps or capability drops) and "
        "is intended for trusted local development only.",
        runtime, runtime, runtime,
    )
    raise ContainerRuntimeUnavailable(
        f"container runtime {runtime!r} is not usable; refusing to start. "
        f"Set PLUGIN_RUNNER_ISOLATION_MODE=subprocess (trusted local development "
        f"only, disables isolation) to opt out."
    )


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


class EnrollmentRequired(RuntimeError):
    """No usable persisted credential and no enrollment token to obtain one.

    Raised instead of re-attempting enrollment with a spent token or booting
    with a dead credential — the message tells an operator how to recover.
    """


async def _enroll(settings: RunnerSettings, registry) -> PluginRunnerClient:
    """Spend the one-time enrollment token and persist the returned secrets.

    Refuses (loudly) when no token is configured: there is nothing to enroll
    with, and silently continuing would only fail later, per-call."""
    if not settings.enrollment_token:
        raise EnrollmentRequired(
            f"runner {settings.runner_id!r} has no usable credential at "
            f"{settings.state_file!r} and no PLUGIN_RUNNER_ENROLLMENT_TOKEN is "
            "set. Mint a fresh enrollment token for this runner in Catlico and "
            "provide it via PLUGIN_RUNNER_ENROLLMENT_TOKEN, then restart."
        )
    client = PluginRunnerClient(settings.catlico_api_url, timeout=settings.http_timeout)
    resp = await client.enroll(_register_body(settings, registry))
    # Persist before serving so a restart resumes instead of re-spending the
    # (now consumed) token. Never log the returned secrets.
    save_state(
        settings.state_file,
        RunnerState(
            credential=resp["runner_credential"],
            push_signing_secret=resp.get("push_signing_secret", ""),
        ),
    )
    logger.info("enrolled runner %s (credentials persisted)", settings.runner_id)
    return client


async def bootstrap_client(settings: RunnerSettings, registry) -> PluginRunnerClient:
    """Return an authenticated client, resuming from persisted state when possible.

    Precedence: a persisted credential wins over the enrollment token, so a
    restart does not re-spend the one-time token. But the saved credential is
    validated against the API first (a cheap ``/sync``). If the API *rejects* it
    (401/403 — exactly what happens once an admin resets the runner to
    ``pending`` and mints a fresh token), the saved credential is dead, so we
    fall back to enrolling with the token. This gives operators a
    recovery path (reset the runner + supply a fresh token) without having to
    hand-delete the state file.

    A transient failure (5xx, connection error) is *not* proof the credential is
    dead and the credential cannot be re-minted, so it is propagated rather than
    discarding the saved state.
    """
    state = load_state(settings.state_file)
    if state is None or not state.is_usable():
        return await _enroll(settings, registry)

    client = PluginRunnerClient(
        settings.catlico_api_url,
        secret=state.credential,
        push_signing_secret=state.push_signing_secret,
        timeout=settings.http_timeout,
    )
    try:
        await client.sync()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in (401, 403):
            raise
        logger.warning(
            "persisted credential for runner %s was rejected (%s); re-enrolling",
            settings.runner_id,
            exc.response.status_code,
        )
        return await _enroll(settings, registry)
    logger.info("resumed runner %s from persisted credential", settings.runner_id)
    return client


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


async def serve(
    settings: RunnerSettings | None = None,
    *,
    runtime_check: RuntimeCheck | None = None,
) -> None:
    settings = settings or load_settings()
    runtime_check = runtime_check or _container_runtime_available
    registry = discover(settings.plugin_dirs)
    logger.info("discovered %d plugin(s)", len(registry.all()))

    sandbox = select_sandbox(settings)
    # Preflight before we enroll or build anything: if container isolation can't
    # work, refuse to start rather than register as healthy and fail every run.
    await _verify_container_runtime(sandbox, runtime_check)
    await _ensure_plugin_images(sandbox, registry)

    # Resume from persisted credentials when available; only enroll (spending
    # the one-time token) when there is no usable saved state. Runs *after* the
    # container-runtime preflight above: a runner with no usable runtime must
    # never enroll/resume and register as healthy.
    client = await bootstrap_client(settings, registry)

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
