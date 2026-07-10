"""Plugin install pipeline: validate → build image → health-check.

Local (Docker-volume) and GitHub installs share this pipeline. Validation and
Dockerfile generation are pure so they are testable without Docker; the image
build shells out to the container runtime and reports progress through the state
sequence so the web UI can poll it.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from plugin_runner.registry import InstalledPlugin, load_plugin

logger = logging.getLogger(__name__)

# Ordered install states surfaced to the web UI (via PluginVersion.status).
STATE_CLONING = "cloning"
STATE_VALIDATING = "validating"
STATE_BUILDING = "building"
STATE_HEALTH_CHECKING = "health_checking"
STATE_INSTALLED = "installed"
STATE_FAILED = "failed"

_LOCKFILES = ("uv.lock", "poetry.lock", "requirements.txt")
_ALLOWED_PERMISSIONS = {
    "read:case", "read:alert", "read:observable",
    "write:case", "write:task", "write:observable", "write:observable_enrichment",
    "write:plugin_result",
}


@dataclass
class InstallResult:
    status: str
    plugin: InstalledPlugin | None = None
    image_tag: str = ""
    errors: list[str] = field(default_factory=list)
    log: str = ""


def validate_manifest(manifest: dict, *, strict: bool = False) -> list[str]:
    """Return a list of validation errors (empty == valid)."""
    errors: list[str] = []
    for required in ("id", "version", "entrypoint"):
        if not manifest.get(required):
            errors.append(f"manifest missing required field: {required}")
    entrypoint = manifest.get("entrypoint", "")
    if entrypoint and ":" not in entrypoint:
        errors.append("entrypoint must be 'module:Class'")
    if not manifest.get("triggers"):
        errors.append("manifest must declare at least one trigger")
    bad_perms = set(manifest.get("permissions", [])) - _ALLOWED_PERMISSIONS
    if bad_perms:
        errors.append(f"unknown permissions requested: {sorted(bad_perms)}")
    timeout = manifest.get("timeout_seconds", 60)
    if not isinstance(timeout, int) or timeout <= 0:
        errors.append("timeout_seconds must be a positive integer")
    return errors


def has_lockfile(directory: Path) -> bool:
    return any((directory / name).exists() for name in _LOCKFILES)


def image_tag(plugin_id: str, version: str) -> str:
    return f"catlico-plugin/{plugin_id}:{version}"


def generate_dockerfile(plugin: InstalledPlugin) -> str:
    """A non-root, dependency-pinned image that runs the shared SDK worker."""
    install_deps = (
        "RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi\n"
    )
    return (
        "FROM python:3.12-slim\n"
        "RUN useradd --uid 65534 --no-create-home nobodyplugin || true\n"
        "WORKDIR /plugin\n"
        "COPY . /plugin\n"
        "RUN pip install --no-cache-dir catlico-plugin-sdk\n"
        f"{install_deps}"
        "ENV PYTHONPATH=/plugin/src:/plugin\n"
        "USER 65534:65534\n"
        # No ENTRYPOINT: the sandbox sets `python -m catlico_plugin_sdk._worker`.
    )


async def build_image(
    directory: Path, tag: str, *, runtime: str = "docker"
) -> tuple[bool, str]:
    """Shell out to ``docker build``. Returns (ok, combined build log)."""
    dockerfile = directory / "Dockerfile.catlico"
    proc = await asyncio.create_subprocess_exec(
        runtime, "build", "-f", str(dockerfile), "-t", tag, str(directory),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return proc.returncode == 0, (out or b"").decode("utf-8", errors="replace")


async def install_local(
    directory: Path,
    *,
    strict: bool = False,
    runtime: str = "docker",
    build: bool = True,
    on_state=None,
) -> InstallResult:
    """Run the pipeline for a plugin directory. ``on_state(state)`` is called at
    each transition so the caller can report progress to Catlico."""
    async def _emit(state: str) -> None:
        if on_state is not None:
            await on_state(state)

    await _emit(STATE_VALIDATING)
    try:
        plugin = load_plugin(directory)
    except Exception as exc:  # noqa: BLE001 — bad manifest -> failed install
        await _emit(STATE_FAILED)
        return InstallResult(status=STATE_FAILED, errors=[f"load failed: {exc}"])

    errors = validate_manifest(plugin.manifest, strict=strict)
    if not has_lockfile(directory):
        msg = "no lockfile (uv.lock/poetry.lock/requirements.txt)"
        if strict:
            errors.append(msg)
        else:
            logger.warning("%s: %s (warn)", plugin.id, msg)
    if errors:
        await _emit(STATE_FAILED)
        return InstallResult(status=STATE_FAILED, plugin=plugin, errors=errors)

    tag = image_tag(plugin.id, plugin.version)
    if build:
        (directory / "Dockerfile.catlico").write_text(generate_dockerfile(plugin))
        await _emit(STATE_BUILDING)
        ok, log = await build_image(directory, tag, runtime=runtime)
        if not ok:
            await _emit(STATE_FAILED)
            return InstallResult(status=STATE_FAILED, plugin=plugin, errors=["image build failed"], log=log)
        await _emit(STATE_HEALTH_CHECKING)

    await _emit(STATE_INSTALLED)
    return InstallResult(status=STATE_INSTALLED, plugin=plugin, image_tag=tag)
