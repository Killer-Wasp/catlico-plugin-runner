"""Tests for the plugin runner: internal API client, sandbox, event dispatch."""
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
