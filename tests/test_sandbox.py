"""Tests for the sandbox runner and plugin execution."""
import pytest
from plugin_runner.sandbox import SandboxRunner, SandboxRunRequest, SandboxRunResult


class TestSandboxRunRequest:
    def test_request_minimal(self):
        req = SandboxRunRequest(
            run_id="run-1",
            plugin_module="test_plugin",
            plugin_class="Plugin",
            event={},
            config={},
            secrets={},
            api_base_url="http://catlico:8000",
            run_token="tok-1",
        )
        assert req.run_id == "run-1"
        assert req.timeout_seconds == 60  # default

    def test_request_defaults(self):
        req = SandboxRunRequest(
            run_id="r1",
            plugin_module="m",
            plugin_class="C",
            event={},
            config={},
            secrets={},
            api_base_url="http://c",
            run_token="t",
        )
        assert req.timeout_seconds == 60  # default from model
        assert req.memory_limit_mb == 256
        assert req.cpu_limit == 1.0


class TestSandboxRunResult:
    def test_success_result(self):
        result = SandboxRunResult(
            run_id="r1",
            status="success",
            result_summary={"verdict": "info"},
            operation_count=2,
        )
        assert result.status == "success"
        assert not result.error

    def test_timeout_result(self):
        result = SandboxRunResult(
            run_id="r1",
            status="timeout",
            error="timed out after 60s",
        )
        assert result.status == "timeout"
        assert result.error == "timed out after 60s"

    def test_skipped_result(self):
        result = SandboxRunResult(
            run_id="r1",
            status="skipped",
            skip_reason="Not applicable",
        )
        assert result.status == "skipped"
        assert result.skip_reason == "Not applicable"
