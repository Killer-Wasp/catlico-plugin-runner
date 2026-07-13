"""Hermetic sandbox e2e: run a real plugin in a real sandbox against a FAKE
runtime API — no Catlico API, no Postgres, no web, no Docker.

This is the "test the execution path without the API" layer. It proves the piece
`e2e/e2e_check.py` can only prove with the full stack up — that a plugin actually
executes in a sandbox and POSTs a well-formed ``PluginResult`` — but does it in a
single self-contained pytest by pointing the plugin's ``ctx.api`` at a tiny stdlib
HTTP stub.

What's real here: the ``SubprocessSandboxRunner``, the SDK worker, the real
``observable-validator`` plugin, and the real ``ctx.api`` HTTP contract
(``/api/internal/plugin-runtime/{progress,results}`` with a ``Bearer`` token).
What's faked: only the runtime API endpoints the plugin calls, captured for
assertion. (The container adapter is exercised by ``test_container_sandbox.py``;
here we use the subprocess adapter so the test needs no Docker.)
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from plugin_runner.sandbox import SandboxRunRequest, SubprocessSandboxRunner

pytestmark = pytest.mark.asyncio

_RUNTIME_PREFIX = "/api/internal/plugin-runtime"
_PLUGIN_SRC = (
    Path(__file__).resolve().parents[2]
    / "catlico-plugins" / "observable-validator" / "src"
)
_RUN_TOKEN = "run-token-under-test"


class _FakeRuntimeAPI:
    """A real listening HTTP server that stands in for Catlico's plugin-runtime
    API. Records every request so the test can assert what the plugin sent."""

    def __init__(self):
        self.requests: list[dict] = []
        captured = self.requests

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # silence
                pass

            def _record_and_reply(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length) if length else b""
                try:
                    body = json.loads(raw) if raw else None
                except ValueError:
                    body = raw.decode(errors="replace")
                captured.append({
                    "method": self.command,
                    "path": self.path,
                    "auth": self.headers.get("Authorization", ""),
                    "body": body,
                })
                payload = json.dumps({"id": "result-1", "ok": True}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                self._record_and_reply()

            def do_GET(self):
                self._record_and_reply()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"

    def __enter__(self) -> "_FakeRuntimeAPI":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def results(self) -> list[dict]:
        return [r for r in self.requests if r["path"] == f"{_RUNTIME_PREFIX}/results"]


def _event(observable_type: str, value: str) -> dict:
    return {
        "event_id": "audit:1",
        "event_type": "observable.created",
        "organisation_id": "org-a",
        "object": {"type": "observable", "id": "obs-1"},
        "data": {"observable_type": observable_type, "data": value},
    }


def _request(api_base_url: str, event: dict) -> SandboxRunRequest:
    return SandboxRunRequest(
        run_id="run-1",
        plugin_module="observable_validator_plugin.plugin",
        plugin_class="ObservableValidatorPlugin",
        event=event,
        plugin_id="observable-validator",
        plugin_version="0.1.0",
        permissions=["read:observable", "write:plugin_result"],
        plugin_path=str(_PLUGIN_SRC),
        timeout_seconds=15,
        api_base_url=api_base_url,
        run_token=_RUN_TOKEN,
    )


requires_plugin = pytest.mark.skipif(
    not _PLUGIN_SRC.is_dir(), reason="observable-validator source not found"
)


@requires_plugin
async def test_valid_ip_posts_valid_result_to_fake_api():
    with _FakeRuntimeAPI() as api:
        result = await SubprocessSandboxRunner().run(_request(api.base_url, _event("ip", "9.9.9.9")))

    assert result.status == "success", result.error
    posted = api.results()
    assert len(posted) == 1, f"expected one POST /results, got {api.requests}"
    body = posted[0]["body"]
    # The plugin ran for real and produced the same verdict the UI would show.
    assert body["verdict"] == "valid"
    assert body["entity_type"] == "observable"
    assert body["normalized_data"]["value"] == "9.9.9.9"
    # And it authenticated with the run-scoped bearer token.
    assert posted[0]["auth"] == f"Bearer {_RUN_TOKEN}"


@requires_plugin
async def test_invalid_ip_posts_invalid_result():
    # Proves the plugin's *logic* runs (not just connectivity): a malformed IP
    # yields the 'invalid' verdict, end-to-end through the sandbox.
    with _FakeRuntimeAPI() as api:
        result = await SubprocessSandboxRunner().run(
            _request(api.base_url, _event("ip", "999.999.999.999"))
        )

    assert result.status == "success", result.error
    posted = api.results()
    assert len(posted) == 1
    assert posted[0]["body"]["verdict"] == "invalid"


@requires_plugin
async def test_unknown_type_posts_not_applicable():
    # A present-but-unknown type has no format to check → 'not-applicable',
    # posted end-to-end through the sandbox.
    with _FakeRuntimeAPI() as api:
        result = await SubprocessSandboxRunner().run(
            _request(api.base_url, _event("other", "whatever"))
        )

    assert result.status == "success", result.error
    posted = api.results()
    assert len(posted) == 1
    assert posted[0]["body"]["verdict"] == "not-applicable"


@requires_plugin
async def test_file_type_skips_without_touching_the_api():
    # 'file' observables are filtered in should_process — the run skips before any
    # ctx.api call, so the fake API records nothing at all.
    with _FakeRuntimeAPI() as api:
        result = await SubprocessSandboxRunner().run(
            _request(api.base_url, _event("file", "malware.exe"))
        )

    assert result.status == "skipped", result.error
    assert api.requests == []
