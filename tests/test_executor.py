"""SubprocessExecutor: real process isolation, timeout, log capture, redaction.

Fixtures are real ``main.py`` files defining a ``Catlico`` app; the executor
spawns the SDK worker (using the runner's own interpreter as the fallback venv
python) to run them in a child process.
"""
import textwrap
from pathlib import Path

import pytest

from plugin_runner.executor import RunRequest, SubprocessExecutor

pytestmark = pytest.mark.asyncio


def _write_app(tmp_path: Path, body: str) -> str:
    """Write a plugin entrypoint ``main.py`` and return its dir (the plugin root)."""
    (tmp_path / "main.py").write_text(textwrap.dedent(body))
    return str(tmp_path)


_EVENT = {
    "event_id": "audit:1",
    "event_type": "observable.created",
    "organisation_id": "org-a",
    "object": {"type": "observable", "id": "obs-1"},
    "data": {"observable_type": "ip", "data": "1.2.3.4"},
}


def _request(plugin_path: str, **overrides) -> RunRequest:
    base = dict(
        run_id="run-1",
        plugin_module="main",
        plugin_object="catlico",
        event=_EVENT,
        plugin_path=plugin_path,
        declared_triggers=["observable.created"],
        timeout_seconds=15,
    )
    base.update(overrides)
    return RunRequest(**base)


_APP_HEADER = """
from catlico_plugin_sdk import Catlico
catlico = Catlico()
"""


async def test_success_captures_log_tail(tmp_path):
    path = _write_app(
        tmp_path,
        _APP_HEADER + """
@catlico.event("observable.created")
async def handle(event, ctx):
    print("hello from plugin")
""",
    )
    result = await SubprocessExecutor().run(_request(path))
    assert result.status == "success", result.error
    assert "hello from plugin" in (result.log_tail or "")


async def test_matcher_reject_is_skipped(tmp_path):
    path = _write_app(
        tmp_path,
        _APP_HEADER + """
@catlico.event("observable.created", matchers=[lambda e: False])
async def handle(event, ctx):
    raise AssertionError("must not run")
""",
    )
    result = await SubprocessExecutor().run(_request(path))
    assert result.status == "skipped"


async def test_typed_error_sets_error_kind(tmp_path):
    path = _write_app(
        tmp_path,
        _APP_HEADER + """
from catlico_plugin_sdk import ConfigError

@catlico.event("observable.created")
async def handle(event, ctx):
    raise ConfigError("bad api key")
""",
    )
    result = await SubprocessExecutor().run(_request(path))
    assert result.status == "failure"
    assert result.error_kind == "config"
    assert "bad api key" in (result.error or "")


async def test_unexpected_exception_is_bug(tmp_path):
    path = _write_app(
        tmp_path,
        _APP_HEADER + """
@catlico.event("observable.created")
async def handle(event, ctx):
    raise ValueError("boom")
""",
    )
    result = await SubprocessExecutor().run(_request(path))
    assert result.status == "failure"
    assert result.error_kind == "bug"


async def test_trigger_mismatch_is_config_failure(tmp_path):
    path = _write_app(
        tmp_path,
        _APP_HEADER + """
@catlico.event("observable.created")
async def handle(event, ctx):
    pass
""",
    )
    result = await SubprocessExecutor().run(
        _request(path, declared_triggers=["observable.created", "case.created"])
    )
    assert result.status == "failure"
    assert result.error_kind == "config"


async def test_health_action_returns_health(tmp_path):
    path = _write_app(
        tmp_path,
        _APP_HEADER + """
@catlico.event("observable.created")
async def handle(event, ctx):
    pass

@catlico.health()
async def health(ctx):
    return {"ok": True, "probe": "done"}
""",
    )
    result = await SubprocessExecutor().run(_request(path, action="health"))
    assert result.status == "success", result.error
    assert result.health == {"ok": True, "probe": "done"}


async def test_timeout_kills_process(tmp_path):
    path = _write_app(
        tmp_path,
        _APP_HEADER + """
import asyncio

@catlico.event("observable.created")
async def handle(event, ctx):
    await asyncio.sleep(30)
""",
    )
    result = await SubprocessExecutor().run(_request(path, timeout_seconds=1))
    assert result.status == "timeout"
    assert result.error_kind == "timeout"


# --- Secret redaction in the log tail (real subprocess) ---

SECRET = "supersecretapikey-abc123XYZ"
TOKEN = "bearer-token-9f8e7d6c5b4a"


def _leak_app(tmp_path, tail: str) -> str:
    return _write_app(
        tmp_path,
        _APP_HEADER + f"""
import sys

@catlico.event("observable.created")
async def handle(event, ctx):
    print("stdout leak: {SECRET}", flush=True)
    print("stderr leak: {SECRET}", file=sys.stderr, flush=True)
    {tail}
""",
    )


async def test_secret_redacted_on_success(tmp_path):
    path = _leak_app(tmp_path, "pass")
    result = await SubprocessExecutor().run(
        _request(path, secrets={"api_key": SECRET})
    )
    assert result.status == "success", result.error
    assert SECRET not in (result.log_tail or "")
    assert "***REDACTED***" in (result.log_tail or "")


async def test_secret_redacted_on_failure(tmp_path):
    path = _leak_app(tmp_path, "raise ValueError('boom')")
    result = await SubprocessExecutor().run(
        _request(path, secrets={"api_key": SECRET})
    )
    assert result.status == "failure"
    assert SECRET not in (result.log_tail or "")


async def test_run_token_redacted(tmp_path):
    path = _write_app(
        tmp_path,
        _APP_HEADER + f"""
@catlico.event("observable.created")
async def handle(event, ctx):
    print("token is {TOKEN}", flush=True)
""",
    )
    result = await SubprocessExecutor().run(_request(path, run_token=TOKEN))
    assert result.status == "success", result.error
    assert TOKEN not in (result.log_tail or "")
    assert "***REDACTED***" in (result.log_tail or "")


async def test_short_and_empty_secrets_do_not_corrupt_log(tmp_path):
    path = _write_app(
        tmp_path,
        _APP_HEADER + """
@catlico.event("observable.created")
async def handle(event, ctx):
    print("processed 1 item ok=true count=0 done", flush=True)
""",
    )
    result = await SubprocessExecutor().run(
        _request(path, secrets={"a": "", "b": "1", "c": "true"})
    )
    assert result.status == "success", result.error
    assert "***REDACTED***" not in (result.log_tail or "")
    assert "processed 1 item ok=true count=0 done" in (result.log_tail or "")


# --- Status validation ---


async def test_unknown_status_coerces_to_failure(tmp_path):
    """Unknown status strings from result file are coerced to failure with original preserved."""
    path = _write_app(
        tmp_path,
        _APP_HEADER + """
import json
from pathlib import Path

@catlico.event("observable.created")
async def handle(event, ctx):
    # Manually write result with invalid status
    # (plugin code can access result_path via ctx.result_path)
    pass
""",
    )
    # We'll test at a lower level by mocking the result file
    from unittest.mock import patch
    import json

    executor = SubprocessExecutor()
    # Create a mock result with an invalid status
    mock_result = {
        "status": "completed",  # Invalid status
        "error": "some error",
    }

    with patch.object(executor, "_read_result", return_value=mock_result):
        result = await executor.run(_request(path))
        assert result.status == "failure", "Invalid status should coerce to failure"
        assert result.error is not None
        assert "invalid status from plugin result" in result.error
        assert "'completed'" in result.error
        assert "some error" in result.error


async def test_valid_statuses_pass_through(tmp_path):
    """All valid statuses (success, failure, timeout, skipped) should pass through unchanged."""
    from unittest.mock import patch

    executor = SubprocessExecutor()
    for valid_status in ["success", "failure", "timeout", "skipped"]:
        # Create a unique directory for each status test
        status_dir = tmp_path / valid_status
        status_dir.mkdir(exist_ok=True)
        mock_result = {"status": valid_status}
        with patch.object(executor, "_read_result", return_value=mock_result):
            result = await executor.run(_request(_write_app(status_dir, _APP_HEADER)))
            assert result.status == valid_status, f"Status {valid_status} should pass through"
