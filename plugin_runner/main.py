"""Runner entrypoint + build-time CLI.

Console-script subcommands (default is ``serve``):

* ``plugin-runner`` / ``plugin-runner serve`` — provision plugin venvs, self-register,
  start the heartbeat loop, and serve the private API.
* ``plugin-runner sync`` — discover + provision every plugin venv, exit non-zero on any
  failure (fail-fast for image builds/CI).
* ``plugin-runner install <source> [--ref REF] [--name NAME]`` — copy a local plugin dir or
  clone a git repo into the plugins dir. Build-stage/dev tool only — no HTTP surface.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import uvicorn

from plugin_runner import venvs
from plugin_runner.client import PluginRunnerClient
from plugin_runner.executor import SubprocessExecutor
from plugin_runner.gitclone import GitCloneError, clone_source
from plugin_runner.metrics import (
    record_heartbeat,
    set_installed_plugin_count,
    set_isolation_mode,
    set_quarantined_plugin_count,
)
from plugin_runner.registry import STATUS_FAILED, STATUS_READY, Registry, discover
from plugin_runner.server import create_app
from plugin_runner.settings import ISOLATION_MODE, RunnerSettings, load_settings

logger = logging.getLogger(__name__)

#: Injectable uv preflight: is ``uv --version`` runnable and returning 0?
UvCheck = Callable[[], Awaitable[bool]]


class UvUnavailable(RuntimeError):
    """Raised at startup when ``uv`` is not usable. Refusing to start is deliberate:
    every plugin venv sync would fail, so the runner must not register as healthy."""


async def _uv_available() -> bool:
    try:
        proc = await asyncio.create_subprocess_exec(
            "uv", "--version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError):
        return False
    try:
        return (await asyncio.wait_for(proc.wait(), timeout=10)) == 0
    except asyncio.TimeoutError:
        proc.kill()
        return False


async def _verify_uv(uv_check: UvCheck) -> None:
    if await uv_check():
        return
    logger.error(
        "uv is not usable (a `uv --version` preflight failed). Every plugin venv "
        "sync would fail. Refusing to start. Install uv and put it on PATH."
    )
    raise UvUnavailable("uv is not usable; refusing to start")


def _fold_venv_results(
    registry: Registry, results: dict[str, venvs.VenvResult]
) -> int:
    """Attach venv pythons to plugins and quarantine sync failures. Returns the
    quarantined count and updates the gauges."""
    quarantined = 0
    for plugin in registry.all():
        result = results.get(plugin.id)
        if result is not None:
            plugin.venv_python = result.python
            if not result.ok:
                plugin.status = STATUS_FAILED
                detail = result.error or "venv sync failed"
                plugin.error = f"{plugin.error}; {detail}" if plugin.error else detail
        if plugin.status != STATUS_READY:
            quarantined += 1
    set_installed_plugin_count(len(registry.all()))
    set_quarantined_plugin_count(quarantined)
    return quarantined


async def _provision(settings: RunnerSettings, *, collect_garbage: bool) -> Registry:
    """Discover plugins and ensure each has a synced venv, folding results.

    ``collect_garbage`` prunes stale venv dirs — passed True at startup only,
    never on rescan (an in-flight run may still be bound to an old venv)."""
    registry = discover(settings.plugins_dir)
    results = await venvs.ensure_all(
        registry.all(),
        venvs_dir=settings.resolved_venvs_dir(),
        uv_cache_dir=settings.resolved_uv_cache_dir(),
        sdk_source=settings.sdk_source,
        timeout=settings.uv_sync_timeout_seconds,
        concurrency=settings.venv_sync_concurrency,
        collect_garbage=collect_garbage,
    )
    quarantined = _fold_venv_results(registry, results)
    logger.info(
        "provisioned %d plugin(s), %d ready, %d quarantined",
        len(registry.all()), len(registry.all()) - quarantined, quarantined,
    )
    return registry


def _register_body(settings: RunnerSettings, registry: Registry) -> dict:
    return {
        "id": settings.runner_id,
        "name": settings.name,
        "version": settings.version,
        "base_url": settings.advertised_url,
        "capabilities": ["enrichment"],
        "isolation_mode": ISOLATION_MODE,
        "plugins": registry.manifests(),
    }


async def _heartbeat_loop(
    client: PluginRunnerClient, settings: RunnerSettings, registry: Registry
) -> None:
    while True:
        try:
            await client.heartbeat(
                {
                    "runner_id": settings.runner_id,
                    "capacity": 10,
                    "active_run_count": 0,
                    "installed_plugin_count": len(registry.all()),
                    "health_summary": "ok",
                }
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — heartbeat must survive API blips
            logger.exception("heartbeat failed")
            record_heartbeat("failure")
        else:
            record_heartbeat("success")
        await asyncio.sleep(settings.heartbeat_interval_seconds)


async def serve(
    settings: RunnerSettings | None = None,
    *,
    uv_check: UvCheck | None = None,
) -> None:
    settings = settings or load_settings()
    uv_check = uv_check or _uv_available
    # Preflight before we provision or enroll: no uv means every sync fails.
    await _verify_uv(uv_check)

    registry = await _provision(settings, collect_garbage=True)
    executor = SubprocessExecutor()
    set_isolation_mode(ISOLATION_MODE)

    # Self-register once at startup (no enrollment exchange), announcing this
    # runner and its plugin manifests. Runs after the uv preflight so a runner
    # with no usable uv never registers as healthy.
    client = PluginRunnerClient(
        settings.catlico_api_url,
        shared_secret=settings.shared_secret,
        runner_id=settings.runner_id,
        timeout=settings.http_timeout,
    )
    await client.register(_register_body(settings, registry))
    logger.info("registered runner %s", settings.runner_id)

    async def _rescan() -> None:
        # Re-provision without GC and swap the live registry atomically.
        fresh = await _provision(settings, collect_garbage=False)
        registry.replace_all(fresh.all())
        logger.info("rescan complete: %d plugin(s)", len(registry.all()))

    app = create_app(
        client=client,
        registry=registry,
        runner_id=settings.runner_id,
        api_base_url=settings.plugin_api_url or settings.catlico_api_url,
        executor=executor,
        push_secret=lambda: settings.shared_secret,
        rescan=_rescan,
    )
    heartbeat = asyncio.create_task(_heartbeat_loop(client, settings, registry))
    config = uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info")
    server = uvicorn.Server(config)
    try:
        await server.serve()
    finally:
        heartbeat.cancel()


# --- CLI subcommands --------------------------------------------------------


async def sync_command(settings: RunnerSettings) -> int:
    """Discover + provision every plugin venv. Exit non-zero on ANY failure."""
    if not await _uv_available():
        logger.error("uv is not usable; cannot sync plugin venvs")
        return 1
    registry = await _provision(settings, collect_garbage=True)
    failed = [p for p in registry.all() if p.status != STATUS_READY]
    for plugin in failed:
        logger.error("plugin %s failed: %s", plugin.id, plugin.error)
    return 1 if failed else 0


_GIT_MARKERS = ("://", "git@")


def _looks_like_git(source: str) -> bool:
    return any(m in source for m in _GIT_MARKERS) or source.endswith(".git")


def _name_from_git_url(source: str) -> str:
    tail = source.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def install_command(
    settings: RunnerSettings, source: str, *, ref: str | None = None, name: str | None = None
) -> int:
    """Copy a local plugin dir or clone a git repo into the plugins dir.

    Build-stage/dev tool only (no HTTP surface). Validates that the result holds a
    ``catlico-plugin.toml``; a clone/copy that doesn't is removed and reported."""
    plugins_dir = Path(settings.plugins_dir)
    plugins_dir.mkdir(parents=True, exist_ok=True)
    src_path = Path(source)

    if src_path.is_dir():
        derived = name or src_path.name
        dest = plugins_dir / derived
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        shutil.copytree(src_path, dest, ignore=shutil.ignore_patterns(".venv", "__pycache__"))
    elif _looks_like_git(source):
        derived = name or _name_from_git_url(source)
        dest = plugins_dir / derived
        try:
            sha = clone_source(source, ref or "HEAD", dest)
        except GitCloneError as exc:
            logger.error("clone failed: %s", exc)
            return 1
        logger.info("cloned %s at %s", source, sha)
    else:
        logger.error("%s is neither a local directory nor a git URL", source)
        return 1

    if not (dest / "catlico-plugin.toml").is_file():
        logger.error("%s has no catlico-plugin.toml — not a plugin; removing", dest)
        shutil.rmtree(dest, ignore_errors=True)
        return 1
    logger.info("installed plugin into %s", dest)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="plugin-runner", description="Catlico plugin runner.")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="provision venvs, register, and serve (default)")
    sub.add_parser("sync", help="discover + provision every plugin venv; non-zero on failure")
    p_install = sub.add_parser("install", help="copy/clone a plugin into the plugins dir")
    p_install.add_argument("source", help="local plugin directory or a git URL")
    p_install.add_argument("--ref", dest="ref", help="git ref (branch/tag/sha) for a git source")
    p_install.add_argument("--name", dest="name", help="override the installed plugin dir name")
    return parser


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO)
    args = build_parser().parse_args(argv)
    settings = load_settings()
    if args.command == "sync":
        raise SystemExit(asyncio.run(sync_command(settings)))
    if args.command == "install":
        raise SystemExit(install_command(settings, args.source, ref=args.ref, name=args.name))
    # Default (no subcommand) and explicit "serve".
    asyncio.run(serve(settings))


if __name__ == "__main__":
    main()
