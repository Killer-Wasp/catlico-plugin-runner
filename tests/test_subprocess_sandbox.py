"""Subprocess sandbox adapter: real process isolation, timeout, log capture."""
import textwrap
from pathlib import Path

import pytest

from plugin_runner.sandbox import (
    SandboxRunRequest,
    SubprocessSandboxRunner,
)

pytestmark = pytest.mark.asyncio


def _write_plugin(tmp_path: Path, body: str) -> str:
    """Write a fake plugin module and return its directory (for sys.path)."""
    module = tmp_path / "fake_plugin.py"
    module.write_text(textwrap.dedent(body))
    return str(tmp_path)


_EVENT = {
    "event_id": "audit:1",
    "event_type": "observable.created",
    "organisation_id": "org-a",
    "object": {"type": "observable", "id": "obs-1"},
    "data": {"observable_type": "ip", "data": "1.2.3.4"},
}


def _request(plugin_path: str, **overrides) -> SandboxRunRequest:
    base = dict(
        run_id="run-1",
        plugin_module="fake_plugin",
        plugin_class="Plugin",
        event=_EVENT,
        plugin_path=plugin_path,
        timeout_seconds=10,
    )
    base.update(overrides)
    return SandboxRunRequest(**base)


async def test_success_captures_log_tail(tmp_path):
    plugin_path = _write_plugin(
        tmp_path,
        """
        from catlico_plugin_sdk import CatlicoPlugin

        class Plugin(CatlicoPlugin):
            async def should_process(self, event, ctx):
                return True

            async def process(self, event, ctx):
                print("hello from plugin")
        """,
    )
    result = await SubprocessSandboxRunner().run(_request(plugin_path))
    assert result.status == "success", result.error
    assert "hello from plugin" in (result.log_tail or "")


async def test_should_process_false_is_skipped(tmp_path):
    plugin_path = _write_plugin(
        tmp_path,
        """
        from catlico_plugin_sdk import CatlicoPlugin

        class Plugin(CatlicoPlugin):
            async def should_process(self, event, ctx):
                return False

            async def process(self, event, ctx):
                raise AssertionError("must not run")
        """,
    )
    result = await SubprocessSandboxRunner().run(_request(plugin_path))
    assert result.status == "skipped"
    assert "should_process" in (result.skip_reason or "")


async def test_typed_error_sets_error_kind(tmp_path):
    plugin_path = _write_plugin(
        tmp_path,
        """
        from catlico_plugin_sdk import CatlicoPlugin, ConfigError

        class Plugin(CatlicoPlugin):
            async def should_process(self, event, ctx):
                return True

            async def process(self, event, ctx):
                raise ConfigError("bad api key")
        """,
    )
    result = await SubprocessSandboxRunner().run(_request(plugin_path))
    assert result.status == "failure"
    assert result.error_kind == "config"
    assert "bad api key" in (result.error or "")


async def test_unexpected_exception_is_bug(tmp_path):
    plugin_path = _write_plugin(
        tmp_path,
        """
        from catlico_plugin_sdk import CatlicoPlugin

        class Plugin(CatlicoPlugin):
            async def should_process(self, event, ctx):
                return True

            async def process(self, event, ctx):
                raise ValueError("boom")
        """,
    )
    result = await SubprocessSandboxRunner().run(_request(plugin_path))
    assert result.status == "failure"
    assert result.error_kind == "bug"


async def test_timeout_kills_process(tmp_path):
    plugin_path = _write_plugin(
        tmp_path,
        """
        import asyncio
        from catlico_plugin_sdk import CatlicoPlugin

        class Plugin(CatlicoPlugin):
            async def should_process(self, event, ctx):
                return True

            async def process(self, event, ctx):
                await asyncio.sleep(30)
        """,
    )
    result = await SubprocessSandboxRunner().run(
        _request(plugin_path, timeout_seconds=1)
    )
    assert result.status == "timeout"
    assert result.error_kind == "timeout"


# --- Secret redaction in the log tail (real subprocess, every terminal path) ---

SECRET = "supersecretapikey-abc123XYZ"
TOKEN = "bearer-token-9f8e7d6c5b4a"

_LEAK_TO_BOTH_STREAMS = """
import sys
from catlico_plugin_sdk import CatlicoPlugin

class Plugin(CatlicoPlugin):
    async def should_process(self, event, ctx):
        return True

    async def process(self, event, ctx):
        print("stdout leak: %s", flush=True)
        print("stderr leak: %s", file=sys.stderr, flush=True)
        {tail}
"""


def _leak_plugin(tmp_path, tail: str) -> str:
    return _write_plugin(
        tmp_path,
        _LEAK_TO_BOTH_STREAMS.format(secret=SECRET, tail=tail).replace("%s", SECRET),
    )


async def test_secret_redacted_on_success(tmp_path):
    plugin_path = _leak_plugin(tmp_path, "pass")
    result = await SubprocessSandboxRunner().run(
        _request(plugin_path, secrets={"api_key": SECRET})
    )
    assert result.status == "success", result.error
    assert SECRET not in (result.log_tail or "")
    assert "***REDACTED***" in (result.log_tail or "")


async def test_secret_redacted_on_failure(tmp_path):
    plugin_path = _leak_plugin(tmp_path, "raise ValueError('boom')")
    result = await SubprocessSandboxRunner().run(
        _request(plugin_path, secrets={"api_key": SECRET})
    )
    assert result.status == "failure"
    assert SECRET not in (result.log_tail or "")
    assert "***REDACTED***" in (result.log_tail or "")  # leak captured + masked


async def test_secret_redacted_on_timeout(tmp_path):
    # The runner's drain-after-kill is best-effort, so the timeout log tail may
    # be empty; either way the contract holds: the secret must never appear.
    plugin_path = _leak_plugin(
        tmp_path, "import asyncio\n        await asyncio.sleep(30)"
    )
    result = await SubprocessSandboxRunner().run(
        _request(plugin_path, timeout_seconds=1, secrets={"api_key": SECRET})
    )
    assert result.status == "timeout"
    assert SECRET not in (result.log_tail or "")


async def test_run_token_redacted(tmp_path):
    plugin_path = _write_plugin(
        tmp_path,
        f"""
        from catlico_plugin_sdk import CatlicoPlugin

        class Plugin(CatlicoPlugin):
            async def should_process(self, event, ctx):
                return True

            async def process(self, event, ctx):
                print("token is {TOKEN}", flush=True)
        """,
    )
    result = await SubprocessSandboxRunner().run(
        _request(plugin_path, run_token=TOKEN)
    )
    assert result.status == "success", result.error
    assert TOKEN not in (result.log_tail or "")
    assert "***REDACTED***" in (result.log_tail or "")


async def test_short_and_empty_secrets_do_not_corrupt_log(tmp_path):
    plugin_path = _write_plugin(
        tmp_path,
        """
        from catlico_plugin_sdk import CatlicoPlugin

        class Plugin(CatlicoPlugin):
            async def should_process(self, event, ctx):
                return True

            async def process(self, event, ctx):
                print("processed 1 item ok=true count=0 done", flush=True)
        """,
    )
    result = await SubprocessSandboxRunner().run(
        _request(plugin_path, secrets={"a": "", "b": "1", "c": "true"})
    )
    assert result.status == "success", result.error
    assert "***REDACTED***" not in (result.log_tail or "")
    assert "processed 1 item ok=true count=0 done" in (result.log_tail or "")


async def test_non_string_secret_does_not_crash(tmp_path):
    plugin_path = _write_plugin(
        tmp_path,
        """
        from catlico_plugin_sdk import CatlicoPlugin

        class Plugin(CatlicoPlugin):
            async def should_process(self, event, ctx):
                return True

            async def process(self, event, ctx):
                print("numeric secret 987654 flag True", flush=True)
        """,
    )
    result = await SubprocessSandboxRunner().run(
        _request(plugin_path, secrets={"num": 987654, "flag": True})
    )
    assert result.status == "success", result.error
    assert "987654" not in (result.log_tail or "")  # coerced + long enough
