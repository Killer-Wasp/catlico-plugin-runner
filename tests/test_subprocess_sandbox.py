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
