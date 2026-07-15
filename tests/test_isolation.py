"""StackStorm isolation lesson: a plugin subprocess must NOT fall back to the
runner's host site-packages.

We build a real venv that has the SDK (so the worker runs) but NOT a runner-only
dependency (``prometheus_client``), then run a plugin whose handler imports that
dependency. If the worker leaked the runner's site-packages onto the plugin's
path, the import would succeed; it must fail instead — proving the plugin's deps
come solely from its own venv.

Slow + real-uv, and skipped cleanly when uv/network is unavailable.
"""
import subprocess
import textwrap
from pathlib import Path

import pytest

from plugin_runner.executor import RunRequest, SubprocessExecutor

pytestmark = [pytest.mark.asyncio, pytest.mark.slow]

_SDK_SOURCE = Path(__file__).resolve().parents[2] / "catlico-plugin-sdk"

_EVENT = {
    "event_id": "audit:1",
    "event_type": "observable.created",
    "organisation_id": "org-a",
    "object": {"type": "observable", "id": "obs-1"},
    "data": {},
}


def _build_sdk_venv(tmp_path: Path) -> Path:
    """A venv with catlico-plugin-sdk installed (and nothing runner-specific)."""
    venv = tmp_path / "plugvenv"
    created = subprocess.run(
        ["uv", "venv", str(venv)], capture_output=True, text=True
    )
    if created.returncode != 0:
        pytest.skip(f"uv venv unavailable: {created.stderr}")
    installed = subprocess.run(
        ["uv", "pip", "install", "--python", str(venv / "bin" / "python"),
         "-e", str(_SDK_SOURCE)],
        capture_output=True, text=True,
    )
    if installed.returncode != 0:
        pytest.skip(f"could not install the SDK into the venv: {installed.stderr}")
    return venv


def _write_plugin(tmp_path: Path, body: str) -> str:
    (tmp_path / "main.py").write_text(textwrap.dedent(body))
    return str(tmp_path)


def _request(path: str, python: str) -> RunRequest:
    return RunRequest(
        run_id="run-1",
        plugin_module="main",
        plugin_object="catlico",
        event=_EVENT,
        plugin_path=path,
        python_executable=python,
        declared_triggers=["observable.created"],
        timeout_seconds=60,
    )


async def test_plugin_cannot_import_a_runner_only_dependency(tmp_path):
    venv = _build_sdk_venv(tmp_path)
    python = str(venv / "bin" / "python")
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    path = _write_plugin(
        plugin_dir,
        """
        from catlico_plugin_sdk import Catlico
        catlico = Catlico()

        @catlico.event("observable.created")
        async def handle(event, ctx):
            import prometheus_client  # a runner dep, NOT in this plugin's venv
        """,
    )
    result = await SubprocessExecutor().run(_request(path, python))
    # No host-site-packages fallback: the import must fail inside the plugin.
    assert result.status == "failure", result.log_tail
    assert "prometheus_client" in ((result.error or "") + (result.log_tail or ""))


async def test_plugin_can_import_its_own_venv_sdk(tmp_path):
    """Control: the SDK IS in the venv, so importing it succeeds — proving the
    failure above is about isolation, not a broken worker."""
    venv = _build_sdk_venv(tmp_path)
    python = str(venv / "bin" / "python")
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir()
    path = _write_plugin(
        plugin_dir,
        """
        from catlico_plugin_sdk import Catlico
        catlico = Catlico()

        @catlico.event("observable.created")
        async def handle(event, ctx):
            import catlico_plugin_sdk  # present in the venv
        """,
    )
    result = await SubprocessExecutor().run(_request(path, python))
    assert result.status == "success", result.log_tail
