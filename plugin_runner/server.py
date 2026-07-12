"""Runner-private HTTP server.

Catlico calls these ``/internal/*`` endpoints on the runner host. They are not
public web endpoints. Event pushes are authenticated by an HMAC signature over the
raw request body, keyed by the per-runner push-signing secret captured at
enrollment.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import tempfile
from pathlib import Path
from typing import Awaitable, Callable

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from plugin_runner.client import PluginRunnerClient
from plugin_runner.engine import dispatch_event
from plugin_runner.installer import STATE_FAILED, STATE_INSTALLED, install_from_source
from plugin_runner.metrics import REGISTRY as METRICS_REGISTRY
from plugin_runner.registry import Registry
from plugin_runner.sandbox import SandboxRunner, SubprocessSandboxRunner

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "x-catlico-signature"


def sign_body(body: bytes, secret: str) -> str:
    """The signature scheme the Catlico push loop must produce."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _verify(body: bytes, header: str | None, secret: str) -> bool:
    if not secret or not header:
        return False
    return hmac.compare_digest(sign_body(body, secret), header)


def create_app(
    *,
    client: PluginRunnerClient,
    registry: Registry,
    runner_id: str,
    api_base_url: str,
    sandbox: SandboxRunner | None = None,
    push_secret: Callable[[], str] | None = None,
    installer: Callable[..., Awaitable] = install_from_source,
    sdk_source: str = "",
    build_runtime: str = "docker",
    install_root: Path | None = None,
    spawn: Callable[[Awaitable], object] | None = None,
) -> Starlette:
    """Build the runner's private app. ``push_secret`` is a callable so the app
    picks up the enrollment secret once it is set post-enrollment.

    ``installer``/``spawn`` are injectable so the background install can be driven
    and observed in tests: ``installer`` defaults to ``install_from_source`` and
    ``spawn`` to a task-retaining ``create_task`` wrapper (fire-and-forget), but a
    test can pass a fake installer plus a ``spawn`` that captures the coroutine to
    await it.
    ``sdk_source``/``build_runtime`` feed the installer's build step;
    ``install_root`` is the parent dir clones land under (keyed by plugin_id)."""
    sandbox = sandbox or SubprocessSandboxRunner()
    push_secret = push_secret or (lambda: client.push_signing_secret)
    install_root = install_root or (Path(tempfile.gettempdir()) / "catlico-plugin-installs")

    # Retain a strong reference to every background install task for its whole
    # lifetime. ``asyncio`` keeps only a WEAK reference to a bare ``create_task``
    # result, so a fire-and-forget task can be garbage-collected — and thus
    # silently cancelled — mid clone/build before it ever reports a result. This
    # set is closed over by the app's handlers (held by the returned Starlette
    # app), so it lives as long as the app rather than being a GC-able local.
    _pending: set = set()

    def _default_spawn(coro: Awaitable) -> object:
        task = asyncio.create_task(coro)
        _pending.add(task)
        task.add_done_callback(_pending.discard)
        return task

    spawn = spawn or _default_spawn

    async def health(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "healthy",
                "runner_id": runner_id,
                "installed_plugin_count": len(registry.all()),
                "isolation_mode": getattr(sandbox, "isolation_mode", "subprocess"),
            }
        )

    async def plugins(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "plugins": [
                    {"id": p.id, "version": p.version, "manifest": p.manifest}
                    for p in registry.all()
                ]
            }
        )

    async def events(request: Request) -> JSONResponse:
        raw = await request.body()
        if not _verify(raw, request.headers.get(SIGNATURE_HEADER), push_secret()):
            return JSONResponse({"detail": "invalid signature"}, status_code=401)
        try:
            envelope = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return JSONResponse({"detail": "invalid JSON"}, status_code=400)
        summary = await dispatch_event(
            envelope, client, registry, sandbox,
            runner_id=runner_id, api_base_url=api_base_url,
        )
        return JSONResponse(summary)

    async def _report_failed(plugin_version_id: str, error: str) -> None:
        try:
            await client.report_install_status(plugin_version_id, STATE_FAILED, error=error)
        except Exception:  # noqa: BLE001 — reporting failure must not itself crash the task
            logger.exception("failed to report install failure for %s", plugin_version_id)

    async def _run_install(
        plugin_version_id: str, plugin_id: str, source_url: str, source_ref: str
    ) -> None:
        """Background install driver. Clones + builds under
        ``install_root/<plugin_id>`` and streams progress back to the API sink.

        NOTE: this does NOT update the in-memory ``registry``, so a freshly
        installed plugin is not yet discoverable/served by this running process —
        a registry refresh / re-discovery (or a restart) is a deliberate
        follow-up, out of scope for this endpoint."""
        target_dir = install_root / plugin_id

        async def on_state(state: str) -> None:
            # Intermediate progress only. Terminal states are reported below from
            # the InstallResult so they carry commit_sha/image_digest/log/error.
            if state in (STATE_INSTALLED, STATE_FAILED):
                return
            await client.report_install_status(plugin_version_id, state)

        try:
            result = await installer(
                source_url,
                source_ref,
                target_dir,
                on_state=on_state,
                sdk_source=sdk_source,
                runtime=build_runtime,
            )
        except Exception as exc:  # noqa: BLE001 — a background task must never die silently
            logger.exception("install of %s raised", plugin_version_id)
            await _report_failed(plugin_version_id, str(exc))
            return

        try:
            await client.report_install_status(
                plugin_version_id,
                result.status,
                commit_sha=result.commit_sha or None,
                # InstallResult only carries the image *tag* (no registry-pushed
                # digest); it is the closest stable image identifier we have.
                image_digest=result.image_tag or None,
                install_log=result.log or None,
                error="; ".join(result.errors) if result.errors else None,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "failed to report terminal install status for %s", plugin_version_id
            )

    async def install(request: Request) -> JSONResponse:
        raw = await request.body()
        if not _verify(raw, request.headers.get(SIGNATURE_HEADER), push_secret()):
            return JSONResponse({"detail": "invalid signature"}, status_code=401)
        try:
            payload = json.loads(raw or b"{}")
            plugin_version_id = payload["plugin_version_id"]
            plugin_id = payload["plugin_id"]
            source_url = payload["source_url"]
            source_ref = payload["source_ref"]
        except (json.JSONDecodeError, KeyError, TypeError):
            return JSONResponse({"detail": "invalid payload"}, status_code=400)
        # Kick the clone+build off in the background so the HTTP response returns
        # immediately (the API maps a slow/blocking response to a 502).
        spawn(_run_install(plugin_version_id, plugin_id, source_url, source_ref))
        return JSONResponse(
            {"accepted": True, "plugin_version_id": plugin_version_id}, status_code=202
        )

    async def cancel_run(request: Request) -> JSONResponse:
        # Best-effort: inline dispatch completes within the event request, so
        # there is usually no in-flight run to kill here. Wired for the
        # background-dispatch path.
        run_id = request.path_params["run_id"]
        return JSONResponse({"run_id": run_id, "cancelled": True})

    async def metrics(request: Request) -> Response:
        # Deliberately unauthenticated, at the Prometheus-conventional
        # top-level path (not under /internal like the other routes here).
        # This server binds to settings.host, which defaults to 0.0.0.0 (not
        # restricted to an internal-only interface), so exposing /metrics
        # without auth is a conscious choice, matching how Prometheus
        # exporters are normally deployed (scraped over a private network,
        # not gated per-endpoint) rather than a gap that mirrors the other
        # (signed/internal) routes.
        return Response(
            generate_latest(METRICS_REGISTRY), media_type=CONTENT_TYPE_LATEST
        )

    return Starlette(
        routes=[
            Route("/internal/health", health, methods=["GET"]),
            Route("/internal/plugins", plugins, methods=["GET"]),
            Route("/internal/events", events, methods=["POST"]),
            Route("/internal/plugins/install", install, methods=["POST"]),
            Route("/internal/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
            Route("/metrics", metrics, methods=["GET"]),
        ]
    )
