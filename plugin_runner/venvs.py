"""Per-plugin dependency venvs — the core of the provisioning model.

Each plugin is a full uv project with a committed ``uv.lock``. This module
materialises one venv per plugin, **content-addressed by ``sha256(uv.lock)``**:
a dependency change is a new venv directory, not an in-place mutation of the old
one (StackStorm's in-place-mutation bug, avoided). A ``.catlico-venv-ok`` marker
records the lock hash a venv was built for, so a warm start with an unchanged
lock does **zero** uv work.

Provisioning is deliberately failure-isolated: one plugin whose sync fails marks
only itself broken (partial dir kept for self-repair on the next sync) and never
stops the runner from starting. Garbage collection of stale venv directories runs
**at startup only** — never mid-flight, where an in-progress run may still be
bound to an old venv.

There is no sandbox: the child ``uv`` process inherits ``os.environ`` (plus a
small overlay), so an operator's internal-index / wheelhouse / TLS settings flow
through to every sync for free.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

#: Written inside a venv dir once the sync (and any sdk_source install) fully
#: succeeds. Its presence + matching contents is the warm-start skip signal.
MARKER_NAME = ".catlico-venv-ok"

#: Tail of captured uv output kept on a failure (bytes).
_UV_LOG_TAIL = 8 * 1024


@dataclass
class UvResult:
    """Outcome of one injectable ``uv`` invocation."""

    returncode: int
    output: str


#: Injectable uv runner: ``(argv, cwd, env, timeout) -> UvResult``. Real impl
#: below; tests pass a fake to assert on calls without shelling out to uv.
RunUv = Callable[..., Awaitable[UvResult]]


@dataclass
class VenvResult:
    plugin_id: str
    python: str          # path to the venv's python (empty on failure)
    ok: bool
    error: str | None = None
    venv_dir: str = ""   # the content-addressed dir (for GC keep-set)


def venv_dir_name(plugin_id: str, lock_sha: str) -> str:
    """Content-addressed venv directory name: ``<id>-<first12 of lock sha256>``."""
    return f"{plugin_id}-{lock_sha[:12]}"


def _lock_sha(plugin_root: Path) -> str | None:
    lock = plugin_root / "uv.lock"
    if not lock.is_file():
        return None
    return hashlib.sha256(lock.read_bytes()).hexdigest()


def _child_env(venv_dir: Path, uv_cache_dir: Path) -> dict[str, str]:
    """``os.environ`` + the overlay that pins uv at this venv/cache.

    Never sanitised: internal-registry (``UV_DEFAULT_INDEX`` / ``UV_INDEX_*``),
    wheelhouse (``UV_FIND_LINKS``), and TLS (``UV_NATIVE_TLS``) settings on the
    runner apply to every sync by design.
    """
    return {
        **os.environ,
        "UV_PROJECT_ENVIRONMENT": str(venv_dir),
        "UV_CACHE_DIR": str(uv_cache_dir),
        "UV_COMPILE_BYTECODE": "1",
    }


async def _default_run_uv(
    argv: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> UvResult:
    """Real uv runner: exec ``argv`` in ``cwd``, capture combined output, cap wall time."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except (FileNotFoundError, OSError) as exc:
        return UvResult(returncode=127, output=f"failed to exec {argv[0]!r}: {exc}")
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return UvResult(returncode=124, output=f"uv timed out after {timeout}s: {' '.join(argv[:3])}")
    return UvResult(returncode=proc.returncode or 0, output=(out or b"").decode("utf-8", "replace"))


def _read_marker(marker: Path) -> dict | None:
    try:
        return json.loads(marker.read_text())
    except (OSError, ValueError):
        return None


def _write_marker(marker: Path, lock_sha: str, sdk_source: str) -> None:
    """Atomic tmp+rename so a crash never leaves a half-written marker."""
    payload = json.dumps({"lock_sha256": lock_sha, "sdk_source": sdk_source})
    tmp = marker.with_name(marker.name + ".tmp")
    tmp.write_text(payload)
    os.replace(tmp, marker)


async def ensure_venv(
    plugin_id: str,
    plugin_root: Path,
    *,
    venvs_dir: Path,
    uv_cache_dir: Path,
    sdk_source: str = "",
    timeout: int = 600,
    run_uv: RunUv = _default_run_uv,
) -> VenvResult:
    """Materialise (or reuse) the content-addressed venv for one plugin.

    Warm path: the marker's ``lock_sha256``/``sdk_source`` match → return with
    **zero** uv calls. Otherwise ``uv sync --frozen --no-dev`` (and, if
    ``sdk_source`` is set, an editable SDK install), then write the marker only on
    full success. A failure keeps the markerless partial dir (self-repairs next
    sync) and returns ``ok=False`` with a captured uv output tail — it never raises.
    """
    # The editable SDK install below runs with cwd=plugin_root (so `uv sync`
    # sees the plugin's own project), which means a *relative* sdk_source like
    # "../catlico-plugin-sdk" would resolve against the plugin's directory and
    # miss. It's documented as relative to the runner's launch dir, so pin it to
    # an absolute path here — before the marker compare, so the recorded value is
    # stable and the warm path still short-circuits.
    if sdk_source:
        sdk_source = str(Path(sdk_source).resolve())

    lock_sha = _lock_sha(plugin_root)
    if lock_sha is None:
        return VenvResult(
            plugin_id, "", False,
            error=f"{plugin_root}/uv.lock is missing (run `uv lock` in the plugin dir)",
        )

    name = venv_dir_name(plugin_id, lock_sha)
    venv_dir = venvs_dir / name
    python = venv_dir / "bin" / "python"
    marker = venv_dir / MARKER_NAME

    if marker.is_file() and python.exists():
        recorded = _read_marker(marker)
        if recorded == {"lock_sha256": lock_sha, "sdk_source": sdk_source}:
            return VenvResult(plugin_id, str(python), True, venv_dir=name)

    venvs_dir.mkdir(parents=True, exist_ok=True)
    uv_cache_dir.mkdir(parents=True, exist_ok=True)
    # A stale marker (lock unchanged but sdk_source flipped, or a prior partial)
    # must not be trusted mid-repair; remove it so failure leaves no ok-marker.
    if marker.exists():
        marker.unlink(missing_ok=True)

    env = _child_env(venv_dir, uv_cache_dir)
    sync = await run_uv(
        ["uv", "sync", "--frozen", "--no-dev"],
        cwd=plugin_root, env=env, timeout=timeout,
    )
    if sync.returncode != 0:
        return VenvResult(
            plugin_id, "", False,
            error=f"uv sync failed (rc={sync.returncode}): {sync.output[-_UV_LOG_TAIL:]}",
            venv_dir=name,
        )

    if sdk_source:
        install = await run_uv(
            ["uv", "pip", "install", "--python", str(python), "-e", sdk_source],
            cwd=plugin_root, env=env, timeout=timeout,
        )
        if install.returncode != 0:
            return VenvResult(
                plugin_id, "", False,
                error=f"editable SDK install failed (rc={install.returncode}): "
                      f"{install.output[-_UV_LOG_TAIL:]}",
                venv_dir=name,
            )

    _write_marker(marker, lock_sha, sdk_source)
    return VenvResult(plugin_id, str(python), True, venv_dir=name)


def gc_stale(venvs_dir: Path, keep: set[str]) -> list[str]:
    """Remove venv directories not in ``keep``. **Startup only** (never mid-flight).

    ``keep`` is the set of ``venv_dir_name`` values for the currently-discovered
    plugins at their current lock hashes. Returns the names removed.
    """
    if not venvs_dir.is_dir():
        return []
    removed: list[str] = []
    for child in venvs_dir.iterdir():
        if not child.is_dir() or child.name in keep:
            continue
        shutil.rmtree(child, ignore_errors=True)
        removed.append(child.name)
    if removed:
        logger.info("garbage-collected %d stale venv(s): %s", len(removed), ", ".join(removed))
    return removed


async def ensure_all(
    plugins: list,
    *,
    venvs_dir: Path,
    uv_cache_dir: Path,
    sdk_source: str = "",
    timeout: int = 600,
    concurrency: int = 4,
    run_uv: RunUv = _default_run_uv,
    collect_garbage: bool = False,
) -> dict[str, VenvResult]:
    """Ensure a venv for every plugin (bounded concurrency), failure-isolated.

    ``plugins`` are objects with ``.id`` and ``.path`` (the plugin ROOT). Returns
    ``{plugin_id: VenvResult}``. When ``collect_garbage`` is set (startup only),
    stale venv dirs are removed afterwards, keeping every current plugin's venv —
    including markerless partials, so a broken plugin's dir survives to self-repair.
    """
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def _one(plugin) -> VenvResult:
        async with semaphore:
            return await ensure_venv(
                plugin.id, Path(plugin.path),
                venvs_dir=venvs_dir, uv_cache_dir=uv_cache_dir,
                sdk_source=sdk_source, timeout=timeout, run_uv=run_uv,
            )

    results = await asyncio.gather(*(_one(p) for p in plugins))
    by_id = {r.plugin_id: r for r in results}

    if collect_garbage:
        keep = {r.venv_dir for r in results if r.venv_dir}
        gc_stale(venvs_dir, keep)
    return by_id
