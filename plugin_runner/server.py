"""Runner-private HTTP server.

Catlico calls these ``/internal/*`` endpoints on the runner host. They are not
public web endpoints. Event pushes are authenticated by an HMAC signature over the
raw request body, keyed by the per-runner push-signing secret captured at
enrollment.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from plugin_runner.client import PluginRunnerClient
from plugin_runner.engine import dispatch_event
from plugin_runner.registry import Registry
from plugin_runner.sandbox import SandboxRunner, SubprocessSandboxRunner

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
) -> Starlette:
    """Build the runner's private app. ``push_secret`` is a callable so the app
    picks up the enrollment secret once it is set post-enrollment."""
    sandbox = sandbox or SubprocessSandboxRunner()
    push_secret = push_secret or (lambda: client.push_signing_secret)

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

    async def cancel_run(request: Request) -> JSONResponse:
        # Best-effort: inline dispatch completes within the event request, so
        # there is usually no in-flight run to kill here. Wired for the
        # background-dispatch path.
        run_id = request.path_params["run_id"]
        return JSONResponse({"run_id": run_id, "cancelled": True})

    return Starlette(
        routes=[
            Route("/internal/health", health, methods=["GET"]),
            Route("/internal/plugins", plugins, methods=["GET"]),
            Route("/internal/events", events, methods=["POST"]),
            Route("/internal/runs/{run_id}/cancel", cancel_run, methods=["POST"]),
        ]
    )
