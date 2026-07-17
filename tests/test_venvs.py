"""Per-plugin venv provisioning: marker skip, re-sync, failure isolation, GC, timeout.

The uv runner is injected so these tests never shell out to uv (except the one
``@pytest.mark.slow`` real-uv test at the bottom).
"""
import json
import types
from pathlib import Path

import pytest

from plugin_runner.venvs import (
    MARKER_NAME,
    UvResult,
    ensure_all,
    ensure_venv,
    gc_stale,
    venv_dir_name,
)


class FakeUv:
    """Records uv invocations; simulates ``uv sync`` creating the venv python."""

    def __init__(self, rc: int = 0, output: str = "ok", create_python: bool = True):
        self.calls: list[list[str]] = []
        self.rc = rc
        self.output = output
        self.create_python = create_python

    async def __call__(self, argv, *, cwd, env, timeout) -> UvResult:
        self.calls.append(argv)
        if self.create_python and self.rc == 0 and argv[:2] == ["uv", "sync"]:
            venv = Path(env["UV_PROJECT_ENVIRONMENT"])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "python").write_text("#!/bin/sh\n")
        return UvResult(self.rc, self.output)


def _plugin_dir(base: Path, name: str = "acme", lock: bytes = b"lock-v1") -> Path:
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "uv.lock").write_bytes(lock)
    return d


def _plugin(base: Path, name: str = "acme", lock: bytes = b"lock-v1"):
    d = _plugin_dir(base, name, lock)
    return types.SimpleNamespace(id=name, path=str(d))


async def _ensure(plugin_root, tmp_path, *, run_uv, sdk_source=""):
    return await ensure_venv(
        "acme", plugin_root,
        venvs_dir=tmp_path / "venvs", uv_cache_dir=tmp_path / "uvcache",
        sdk_source=sdk_source, timeout=60, run_uv=run_uv,
    )


# --- naming ---


def test_venv_dir_name_is_content_addressed():
    assert venv_dir_name("acme", "abcdef0123456789") == "acme-abcdef012345"


# --- cold sync + warm skip ---


async def test_cold_sync_then_warm_skip_makes_zero_uv_calls(tmp_path):
    root = _plugin_dir(tmp_path, "acme")
    uv = FakeUv()

    first = await _ensure(root, tmp_path, run_uv=uv)
    assert first.ok
    assert first.python.endswith("/bin/python")
    assert len(uv.calls) == 1  # one `uv sync`
    marker = Path(first.python).parents[1] / MARKER_NAME
    assert json.loads(marker.read_text())["lock_sha256"]

    # Second call with the same lock: marker + python present -> ZERO uv calls.
    second = await _ensure(root, tmp_path, run_uv=uv)
    assert second.ok
    assert len(uv.calls) == 1  # unchanged — no new uv invocation


async def test_marker_deleted_triggers_resync(tmp_path):
    root = _plugin_dir(tmp_path, "acme")
    uv = FakeUv()
    first = await _ensure(root, tmp_path, run_uv=uv)
    (Path(first.python).parents[1] / MARKER_NAME).unlink()

    await _ensure(root, tmp_path, run_uv=uv)
    assert len(uv.calls) == 2  # markerless -> re-synced


async def test_lock_change_is_a_new_venv(tmp_path):
    root = _plugin_dir(tmp_path, "acme", lock=b"lock-v1")
    uv = FakeUv()
    first = await _ensure(root, tmp_path, run_uv=uv)

    (root / "uv.lock").write_bytes(b"lock-v2")  # dependency change
    second = await _ensure(root, tmp_path, run_uv=uv)
    assert second.python != first.python  # different content-addressed dir
    assert len(uv.calls) == 2


# --- failure isolation ---


async def test_missing_lock_is_hard_fail_without_uv(tmp_path):
    root = tmp_path / "nolock"
    root.mkdir()
    uv = FakeUv()
    result = await _ensure(root, tmp_path, run_uv=uv)
    assert not result.ok
    assert "uv.lock" in result.error
    assert uv.calls == []


async def test_sync_failure_keeps_partial_dir_without_marker(tmp_path):
    root = _plugin_dir(tmp_path, "acme")
    uv = FakeUv(rc=1, output="resolution failed: boom", create_python=False)
    result = await _ensure(root, tmp_path, run_uv=uv)
    assert not result.ok
    assert "boom" in result.error
    venv_dir = tmp_path / "venvs" / result.venv_dir
    assert not (venv_dir / MARKER_NAME).exists()  # no ok-marker on failure


async def test_ensure_all_isolates_one_broken_plugin(tmp_path):
    good = _plugin(tmp_path, "good")
    bad = _plugin(tmp_path, "bad")

    async def run_uv(argv, *, cwd, env, timeout):
        # 'bad' fails; 'good' succeeds and gets a python.
        if str(cwd).endswith("/bad"):
            return UvResult(1, "bad sync failed")
        venv = Path(env["UV_PROJECT_ENVIRONMENT"])
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin" / "python").write_text("#!/bin/sh\n")
        return UvResult(0, "ok")

    results = await ensure_all(
        [good, bad],
        venvs_dir=tmp_path / "venvs", uv_cache_dir=tmp_path / "uvcache",
        run_uv=run_uv,
    )
    assert results["good"].ok
    assert not results["bad"].ok


async def test_timeout_returns_failure(tmp_path):
    root = _plugin_dir(tmp_path, "acme")
    uv = FakeUv(rc=124, output="uv timed out after 60s", create_python=False)
    result = await _ensure(root, tmp_path, run_uv=uv)
    assert not result.ok
    assert "124" in result.error


# --- sdk_source flip ---


async def test_sdk_source_flip_rebuilds_and_installs_editable(tmp_path):
    root = _plugin_dir(tmp_path, "acme")
    uv = FakeUv()

    # Built without sdk_source first.
    first = await _ensure(root, tmp_path, run_uv=uv)
    marker = Path(first.python).parents[1] / MARKER_NAME
    assert json.loads(marker.read_text())["sdk_source"] == ""
    assert len(uv.calls) == 1

    # Flip sdk_source on: marker mismatch -> re-sync + editable SDK install.
    second = await _ensure(root, tmp_path, run_uv=uv, sdk_source="/path/to/sdk")
    assert second.ok
    assert ["uv", "sync", "--frozen", "--no-dev"] in uv.calls
    assert any(c[:3] == ["uv", "pip", "install"] for c in uv.calls)
    assert json.loads(marker.read_text())["sdk_source"] == "/path/to/sdk"


async def test_relative_sdk_source_is_resolved_to_absolute(tmp_path):
    # The editable install runs with cwd=plugin_root, so a relative sdk_source
    # would resolve against the plugin dir and miss the sibling SDK checkout. It
    # must be pinned to an absolute path (against the runner's CWD) before use.
    root = _plugin_dir(tmp_path, "acme")
    uv = FakeUv()

    result = await _ensure(root, tmp_path, run_uv=uv, sdk_source="../some-sdk")
    assert result.ok

    install = next(c for c in uv.calls if c[:3] == ["uv", "pip", "install"])
    sdk_arg = install[install.index("-e") + 1]
    assert Path(sdk_arg).is_absolute(), sdk_arg
    assert sdk_arg == str(Path("../some-sdk").resolve())
    # ...and the marker records the resolved (absolute) form, so the warm path
    # short-circuits instead of rebuilding every startup.
    marker = Path(result.python).parents[1] / MARKER_NAME
    assert json.loads(marker.read_text())["sdk_source"] == str(Path("../some-sdk").resolve())


async def test_sdk_install_failure_is_reported(tmp_path):
    root = _plugin_dir(tmp_path, "acme")

    async def run_uv(argv, *, cwd, env, timeout):
        if argv[:2] == ["uv", "sync"]:
            venv = Path(env["UV_PROJECT_ENVIRONMENT"])
            (venv / "bin").mkdir(parents=True, exist_ok=True)
            (venv / "bin" / "python").write_text("#!/bin/sh\n")
            return UvResult(0, "ok")
        return UvResult(1, "editable install exploded")  # the pip install step

    result = await _ensure(root, tmp_path, run_uv=run_uv, sdk_source="/path/to/sdk")
    assert not result.ok
    assert "editable SDK install failed" in result.error


# --- GC (startup only) ---


def test_gc_removes_stale_venvs_keeping_current(tmp_path):
    venvs_dir = tmp_path / "venvs"
    for name in ("acme-aaa", "acme-bbb", "old-ccc"):
        (venvs_dir / name).mkdir(parents=True)
    removed = gc_stale(venvs_dir, keep={"acme-aaa"})
    assert set(removed) == {"acme-bbb", "old-ccc"}
    assert (venvs_dir / "acme-aaa").is_dir()
    assert not (venvs_dir / "acme-bbb").exists()


async def test_ensure_all_gc_keeps_all_current_including_partial(tmp_path):
    good = _plugin(tmp_path, "good")
    bad = _plugin(tmp_path, "bad")
    venvs_dir = tmp_path / "venvs"
    # A pre-existing stale dir that no current plugin owns.
    (venvs_dir / "stale-xyz").mkdir(parents=True)

    async def run_uv(argv, *, cwd, env, timeout):
        if str(cwd).endswith("/bad"):
            # markerless partial dir must survive GC to self-repair next time.
            Path(env["UV_PROJECT_ENVIRONMENT"]).mkdir(parents=True, exist_ok=True)
            return UvResult(1, "fail")
        venv = Path(env["UV_PROJECT_ENVIRONMENT"])
        (venv / "bin").mkdir(parents=True, exist_ok=True)
        (venv / "bin" / "python").write_text("#!/bin/sh\n")
        return UvResult(0, "ok")

    results = await ensure_all(
        [good, bad],
        venvs_dir=venvs_dir, uv_cache_dir=tmp_path / "uvcache",
        run_uv=run_uv, collect_garbage=True,
    )
    assert not (venvs_dir / "stale-xyz").exists()  # GC'd
    assert (venvs_dir / results["good"].venv_dir).is_dir()
    assert (venvs_dir / results["bad"].venv_dir).is_dir()  # partial kept


# --- real uv (slow) ---


@pytest.mark.slow
async def test_real_uv_sync_materialises_a_venv(tmp_path):
    # A trivial, dependency-free uv project.
    proj = tmp_path / "trivial"
    pkg = proj / "src" / "trivial_plugin"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (proj / "pyproject.toml").write_text(
        "[project]\n"
        'name = "catlico-trivial-plugin"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.14"\n'
        "dependencies = []\n\n"
        "[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        "[tool.hatch.build.targets.wheel]\n"
        'packages = ["src/trivial_plugin"]\n'
    )
    # Produce a real uv.lock; skip if uv can't (e.g. offline/no uv).
    import subprocess

    lock = subprocess.run(["uv", "lock"], cwd=proj, capture_output=True, text=True)
    if lock.returncode != 0:
        pytest.skip(f"uv lock unavailable: {lock.stderr}")

    result = await ensure_venv(
        "trivial", proj,
        venvs_dir=tmp_path / "venvs", uv_cache_dir=tmp_path / "uvcache",
        timeout=300,
    )
    assert result.ok, result.error
    assert Path(result.python).exists()
    assert (Path(result.python).parents[1] / MARKER_NAME).is_file()
