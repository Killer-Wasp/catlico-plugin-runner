"""Tests for the plugin runner: internal API client, sandbox, event dispatch."""
import json

import httpx
import pytest
from plugin_runner.client import PluginRunnerClient


class TestPluginRunnerClient:
    def test_client_builds_correct_headers(self):
        client = PluginRunnerClient(
            base_url="http://catlico:8000",
            shared_secret="test-secret",
            runner_id="runner-1",
        )
        headers = client._headers()
        assert headers["Authorization"] == "Bearer test-secret"
        assert headers["X-Runner-Id"] == "runner-1"

    def test_register_url(self):
        client = PluginRunnerClient(base_url="http://catlico:8000", shared_secret="s")
        assert client._url("/register") == "http://catlico:8000/api/internal/plugin-runner/register"

    def test_heartbeat_url(self):
        client = PluginRunnerClient(base_url="http://catlico:8000/api", shared_secret="s")
        assert client._url("/heartbeat") == "http://catlico:8000/api/api/internal/plugin-runner/heartbeat"


async def test_submit_result_posts_body_with_auth():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw_path"] = request.url.raw_path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = PluginRunnerClient(
        "http://catlico:8000",
        shared_secret="cred",
        runner_id="runner-1",
        transport=httpx.MockTransport(handler),
    )
    await client.submit_result("run-1", {"status": "success", "log_tail": "ok"})

    assert captured["auth"] == "Bearer cred"
    assert captured["raw_path"] == b"/api/internal/plugin-runner/runs/run-1/result"
    assert captured["body"]["status"] == "success"


async def test_claim_run_outcomes():
    cases = {
        "created": httpx.Response(200, json={"status": "queued", "run_id": "r1", "runtime_token": "t1"}),
        "skipped": httpx.Response(200, json={"status": "skipped", "skip_reason": "fresh_result"}),
        "duplicate": httpx.Response(409, json={"detail": "exists"}),
        "deferred": httpx.Response(429, json={"detail": "at cap"}),
    }
    for expected, response in cases.items():
        client = PluginRunnerClient(
            "http://catlico:8000",
            shared_secret="s",
            runner_id="runner-1",
            transport=httpx.MockTransport(lambda req, r=response: r),
        )
        out = await client.claim_run({"event_id": "e", "plugin_id": "p"})
        assert out["outcome"] == expected
        if expected == "created":
            assert out["run_id"] == "r1"
            assert out["runtime_token"] == "t1"
        if expected == "skipped":
            assert out["skip_reason"] == "fresh_result"
