"""Runner-private HTTP server.

Catlico calls these ``/internal/*`` endpoints on the runner host. They are not
public web endpoints. Event (and rescan) pushes are authenticated by an HMAC
signature over the raw request body, keyed by the shared secret configured on
both the API and the runner.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Awaitable, Callable

from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from plugin_runner.client import PluginRunnerClient
from plugin_runner.engine import dispatch_event
from plugin_runner.executor import PluginExecutor, SubprocessExecutor
from plugin_runner.metrics import REGISTRY as METRICS_REGISTRY
from plugin_runner.registry import Registry

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
    executor: PluginExecutor | None = None,
    push_secret: Callable[[], str] | None = None,
    rescan: Callable[[], Awaitable[None]] | None = None,
    spawn: Callable[[Awaitable], object] | None = None,
) -> Starlette:
    """Build the runner's private app.

    ``push_secret`` resolves the shared secret used to verify inbound event/rescan
    push signatures (``main.serve`` passes ``lambda: settings.shared_secret``).
    ``rescan`` is the discover→sync→``replace_all`` provisioning coroutine invoked
    by ``POST /internal/plugins/rescan`` — implemented here but deliberately
    unwired from the API/web in this pass. ``spawn`` is injectable so a test can
    capture and await the background rescan task.
    """
    executor = executor or SubprocessExecutor()
    push_secret = push_secret or (lambda: getattr(client, "push_signing_secret", ""))

    # Retain a strong reference to every background task for its whole lifetime;
    # asyncio keeps only a weak ref to a bare create_task result.
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
                "isolation_mode": getattr(executor, "isolation_mode", "subprocess"),
            }
        )

    async def plugins(request: Request) -> JSONResponse:
        return JSONResponse(
            {
                "plugins": [
                    {
                        "id": p.id,
                        "version": p.version,
                        "status": p.status,
                        "error": p.error,
                        "manifest": p.manifest,
                    }
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
            envelope, client, registry, executor,
            runner_id=runner_id, api_base_url=api_base_url,
        )
        return JSONResponse(summary)

    async def rescan_endpoint(request: Request) -> JSONResponse:
        """Re-discover + re-sync plugins and atomically swap the live registry.

        Implemented and functional, but intentionally unwired: no API proxy route
        or web button calls it in this pass (rescan is restart-only for operators).
        Runs in the background (no GC — an in-flight run may still use an old venv)
        and returns 202 immediately."""
        raw = await request.body()
        if not _verify(raw, request.headers.get(SIGNATURE_HEADER), push_secret()):
            return JSONResponse({"detail": "invalid signature"}, status_code=401)
        if rescan is None:
            return JSONResponse({"detail": "rescan not configured"}, status_code=501)

        async def _run_rescan() -> None:
            try:
                await rescan()
            except Exception:  # noqa: BLE001 — a background task must never die silently
                logger.exception("rescan failed")

        spawn(_run_rescan())
        return JSONResponse({"accepted": True}, status_code=202)

    async def cancel_run(request: Request) -> JSONResponse:
        run_id = request.path_params["run_id"]
        return JSONResponse({"run_id": run_id, "cancelled": True})

    async def metrics(request: Request) -> Response:
        # Deliberately unauthenticated, at the Prometheus-conventional top-level
        # path (not under /internal), matching how exporters are normally scraped.
        return Response(
            generate_latest(METRICS_REGISTRY), media_type=CONTENT_TYPE_LATEST
        )

    return Starlette(
        routes=[
            Route("/internal/health", health, methods=["GET"]),
            Route("/internal/plugins", plugins, methods=["GET"]),
            Route("/internal/events", events, methods=["POST"]),
            Route("/internal/plugins/rescan", rescan_endpoint, methods=["POST"]),
            Route("/internal/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
            Route("/metrics", metrics, methods=["GET"]),
        ]
    )
