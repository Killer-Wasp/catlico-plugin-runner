"""Tests for the plugin runner: internal API client, sandbox, event dispatch."""
import json

import httpx
import pytest
from plugin_runner.client import PluginRunnerClient


class TestPluginRunnerClient:
    def test_client_builds_correct_headers(self):
        client = PluginRunnerClient(
            base_url="http://catlico:8000",
            secret="test-secret",
        )
        headers = client._headers()
        assert headers["Authorization"] == "Bearer test-secret"

    def test_register_url(self):
        client = PluginRunnerClient(base_url="http://catlico:8000", secret="s")
        assert client._url("/register") == "http://catlico:8000/api/internal/plugin-runner/register"

    def test_heartbeat_url(self):
        client = PluginRunnerClient(base_url="http://catlico:8000/api", secret="s")
        assert client._url("/heartbeat") == "http://catlico:8000/api/api/internal/plugin-runner/heartbeat"


async def test_report_install_status_posts_encoded_url_and_body():
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["raw_path"] = request.url.raw_path
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"ok": True})

    client = PluginRunnerClient(
        "http://catlico:8000", secret="cred", transport=httpx.MockTransport(handler)
    )
    # id contains both '@' and '/', which must be percent-encoded as one segment.
    await client.report_install_status(
        "acme/net@1.2.0",
        "building",
        commit_sha="deadbeef",
        image_digest="catlico-plugin/acme:1.2.0",
        install_log="log-tail",
    )

    assert captured["auth"] == "Bearer cred"
    assert captured["raw_path"] == (
        b"/api/internal/plugin-runner/plugins/acme%2Fnet%401.2.0/install-status"
    )
    assert captured["body"] == {
        "state": "building",
        "commit_sha": "deadbeef",
        "image_digest": "catlico-plugin/acme:1.2.0",
        "install_log": "log-tail",
        "error": None,
    }
