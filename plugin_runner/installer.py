"""Plugin install pipeline: validate → build image → health-check.

Local (Docker-volume) and GitHub installs share this pipeline. Validation and
Dockerfile generation are pure so they are testable without Docker; the image
build shells out to the container runtime and reports progress through the state
sequence so the web UI can poll it.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from plugin_runner.registry import InstalledPlugin, load_plugin

logger = logging.getLogger(__name__)

#: Name of the SDK copy staged inside a plugin's build context (see _stage_sdk).
_SDK_STAGE_DIRNAME = ".catlico-sdk"
#: Heavy/irrelevant trees excluded when copying the local SDK checkout.
_SDK_COPY_IGNORE = shutil.ignore_patterns(
    ".venv", "venv", "__pycache__", "*.pyc", ".git", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "*.egg-info",
)

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
    commit_sha: str = ""


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


def generate_dockerfile(plugin: InstalledPlugin, *, sdk_dir: str = "") -> str:
    """A non-root, dependency-pinned image that runs the shared SDK worker.

    ``sdk_dir`` is the build-context-relative directory holding a staged local
    SDK checkout (see ``_stage_sdk``). When set, the SDK is installed from that
    copy and removed from the image afterwards; when empty, the published
    ``catlico-plugin-sdk`` is pulled from PyPI (the production path).
    """
    if sdk_dir:
        install_sdk = (
            f"RUN pip install --no-cache-dir /plugin/{sdk_dir} && rm -rf /plugin/{sdk_dir}\n"
        )
    else:
        install_sdk = "RUN pip install --no-cache-dir catlico-plugin-sdk\n"
    install_deps = (
        "RUN if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt; fi\n"
    )
    return (
        # 3.14+: the SDK and plugins declare requires-python >=3.14 (matches the
        # workspace toolchain); an older base fails `pip install` on that marker.
        "FROM python:3.14-slim\n"
        "RUN useradd --uid 65534 --no-create-home nobodyplugin || true\n"
        "WORKDIR /plugin\n"
        "COPY . /plugin\n"
        f"{install_sdk}"
        f"{install_deps}"
        "ENV PYTHONPATH=/plugin/src:/plugin\n"
        "USER 65534:65534\n"
        # No ENTRYPOINT: the sandbox sets `python -m catlico_plugin_sdk._worker`.
    )


def _stage_sdk(context: Path, sdk_source: str) -> str | None:
    """Copy a local SDK checkout into the plugin's build context.

    Docker can only ``COPY`` from inside the build context, but the local SDK is
    a sibling checkout outside it. Copy it to ``<context>/.catlico-sdk`` so the
    generated Dockerfile can install it, and return that context-relative name.
    Returns ``None`` (image falls back to the PyPI SDK) when no source is
    configured or the path is not a valid checkout. Always paired with a
    ``_unstage_sdk`` in a ``finally``.
    """
    if not sdk_source:
        return None
    src = Path(sdk_source).expanduser()
    if not (src / "pyproject.toml").is_file():
        logger.warning(
            "PLUGIN_RUNNER_SDK_SOURCE=%s is not an SDK checkout (no pyproject.toml); "
            "falling back to the PyPI SDK",
            sdk_source,
        )
        return None
    dest = context / _SDK_STAGE_DIRNAME
    _unstage_sdk(context)  # clear any stale copy from an interrupted build
    shutil.copytree(src, dest, ignore=_SDK_COPY_IGNORE)
    return _SDK_STAGE_DIRNAME


def _unstage_sdk(context: Path) -> None:
    """Remove a staged SDK copy; no-op if absent."""
    shutil.rmtree(context / _SDK_STAGE_DIRNAME, ignore_errors=True)


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


class GitCloneError(RuntimeError):
    """Raised when cloning plugin source from git or resolving its ref fails."""


def _default_git_run(argv: list[str]) -> subprocess.CompletedProcess:
    """Real subprocess runner used by ``clone_source``. Never invoked with
    ``shell=True``; ``argv`` is always a literal list, so untrusted URLs/refs
    can't reach a shell even though this ultimately execs ``git``."""
    return subprocess.run(argv, capture_output=True, text=True)


def clone_source(
    source_url: str,
    source_ref: str,
    dest: Path,
    *,
    run=_default_git_run,
) -> str:
    """Clone ``source_url`` at ``source_ref`` into ``dest``, returning the
    resolved 40-char commit SHA at HEAD.

    Supported ``source_ref`` kinds:
      - branch or tag name: satisfied by the fast path, a shallow
        ``git clone --depth 1 --branch <ref>`` (single-commit fetch).
      - raw commit SHA (full or abbreviated): ``--branch`` cannot target a
        commit, so the shallow clone predictably fails and this falls back to
        a full clone followed by ``git checkout <ref>``, which accepts any
        ref. This full clone fetches the whole history, so it is slower.

    Every git invocation is an argv list passed to ``run`` (default: a plain
    ``subprocess.run`` wrapper) — never ``shell=True`` and never string
    interpolation into a shell command — so a malicious ``source_url`` or
    ``source_ref`` (e.g. containing shell metacharacters) cannot execute
    anything beyond being passed as a literal git argument. ``run`` is
    injectable so tests can point this at a local ``file://`` checkout with
    no network, or fake failures without shelling out at all.

    Raises ``GitCloneError`` with the underlying git stderr/stdout on any
    failure (bad URL, unresolvable ref, corrupt repo, ...).
    """
    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    shallow = run(["git", "clone", "--depth", "1", "--branch", source_ref, source_url, str(dest)])
    if shallow.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        full = run(["git", "clone", source_url, str(dest)])
        if full.returncode != 0:
            raise GitCloneError(
                f"git clone of {source_url!r} failed: "
                f"{(full.stderr or full.stdout or '').strip()}"
            )
        checkout = run(["git", "-C", str(dest), "checkout", source_ref])
        if checkout.returncode != 0:
            raise GitCloneError(
                f"git checkout of ref {source_ref!r} in {source_url!r} failed: "
                f"{(checkout.stderr or checkout.stdout or '').strip()}"
            )

    rev = run(["git", "-C", str(dest), "rev-parse", "HEAD"])
    if rev.returncode != 0:
        raise GitCloneError(f"git rev-parse HEAD failed: {(rev.stderr or '').strip()}")
    sha = rev.stdout.strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        raise GitCloneError(f"unexpected output from git rev-parse HEAD: {sha!r}")
    return sha


async def install_local(
    directory: Path,
    *,
    strict: bool = False,
    runtime: str = "docker",
    build: bool = True,
    sdk_source: str = "",
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
        sdk_dir = _stage_sdk(directory, sdk_source)
        try:
            (directory / "Dockerfile.catlico").write_text(
                generate_dockerfile(plugin, sdk_dir=sdk_dir or "")
            )
            await _emit(STATE_BUILDING)
            ok, log = await build_image(directory, tag, runtime=runtime)
        finally:
            _unstage_sdk(directory)
        if not ok:
            await _emit(STATE_FAILED)
            return InstallResult(status=STATE_FAILED, plugin=plugin, errors=["image build failed"], log=log)
        await _emit(STATE_HEALTH_CHECKING)

    await _emit(STATE_INSTALLED)
    return InstallResult(status=STATE_INSTALLED, plugin=plugin, image_tag=tag)


async def install_from_source(
    source_url: str,
    source_ref: str,
    target_dir: Path,
    *,
    clone=clone_source,
    strict: bool = False,
    runtime: str = "docker",
    build: bool = True,
    sdk_source: str = "",
    on_state=None,
) -> InstallResult:
    """GitHub (or any git remote) install path: clone ``source_url`` at
    ``source_ref`` into ``target_dir``, then run the same
    validate -> build -> health-check pipeline as ``install_local``.

    A future HTTP install endpoint (not part of this change — see
    ``plugin_runner/server.py``) is the intended caller: once it knows a
    ``PluginVersion``'s ``source_url``/``source_ref``, it calls this with a
    scratch ``target_dir`` and streams ``on_state`` transitions back as
    ``PluginVersion.status``.

    ``clone`` is injectable (defaults to ``clone_source``) so callers/tests
    can fake the clone step entirely. Mirrors ``install_local``: a clone
    failure never raises out of this function — it emits ``STATE_FAILED`` and
    returns a failed ``InstallResult``, same as a validation or build failure.
    """
    async def _emit(state: str) -> None:
        if on_state is not None:
            await on_state(state)

    await _emit(STATE_CLONING)
    try:
        commit_sha = await asyncio.to_thread(clone, source_url, source_ref, target_dir)
    except Exception as exc:  # noqa: BLE001 — any clone failure -> failed install
        await _emit(STATE_FAILED)
        return InstallResult(status=STATE_FAILED, errors=[f"clone failed: {exc}"])

    result = await install_local(
        target_dir,
        strict=strict,
        runtime=runtime,
        build=build,
        sdk_source=sdk_source,
        on_state=on_state,
    )
    result.commit_sha = commit_sha
    return result


def _plugin_root(plugin: InstalledPlugin) -> Path:
    """Build context for a plugin. ``InstalledPlugin.path`` points at the import
    root, which is ``<root>/src`` under the standard layout; the Docker build
    context must be the project root that holds the manifest and lockfile."""
    p = Path(plugin.path)
    return p.parent if p.name == "src" else p


async def image_exists(tag: str, *, runtime: str = "docker") -> bool:
    """True if the image tag is already present locally (``docker image inspect``)."""
    proc = await asyncio.create_subprocess_exec(
        runtime, "image", "inspect", tag,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    return (await proc.wait()) == 0


async def _default_build(
    plugin: InstalledPlugin, tag: str, *, runtime: str = "docker", sdk_source: str = ""
) -> bool:
    """Generate the Dockerfile and build the plugin's image. Returns ok."""
    directory = _plugin_root(plugin)
    sdk_dir = _stage_sdk(directory, sdk_source)
    try:
        (directory / "Dockerfile.catlico").write_text(
            generate_dockerfile(plugin, sdk_dir=sdk_dir or "")
        )
        ok, log = await build_image(directory, tag, runtime=runtime)
    finally:
        _unstage_sdk(directory)
    if not ok:
        logger.error("build failed for %s:\n%s", tag, log)
    return ok


async def ensure_images(
    plugins,
    *,
    runtime: str = "docker",
    sdk_source: str = "",
    exists=None,
    build=None,
) -> dict[str, str]:
    """Idempotently ensure every plugin has its per-plugin image built.

    For each plugin: validate the manifest, skip the build if the image tag
    already exists (``exists(tag)``), otherwise build it (``build(plugin, tag)``).
    ``exists`` and ``build`` are injectable so unit tests need no Docker.

    Per-plugin failures are isolated: a bad manifest, a failed build, or a
    raising builder marks only that plugin ``failed`` and never propagates, so a
    single broken plugin cannot stop the runner from starting. Returns a mapping
    of ``plugin_id -> final state`` (``installed`` or ``failed``).
    """
    if exists is None:
        async def exists(tag: str) -> bool:  # noqa: E306
            return await image_exists(tag, runtime=runtime)
    if build is None:
        async def build(plugin: InstalledPlugin, tag: str) -> bool:  # noqa: E306
            return await _default_build(plugin, tag, runtime=runtime, sdk_source=sdk_source)

    states: dict[str, str] = {}
    for plugin in plugins:
        states[plugin.id] = await _ensure_one_image(plugin, exists=exists, build=build)
    return states


async def _ensure_one_image(plugin: InstalledPlugin, *, exists, build) -> str:
    """Run the validate -> (skip | build) pipeline for one plugin, never raising."""
    logger.info("%s: %s", plugin.id, STATE_VALIDATING)
    errors = validate_manifest(plugin.manifest)
    if errors:
        logger.error("%s: %s — %s", plugin.id, STATE_FAILED, "; ".join(errors))
        return STATE_FAILED

    tag = image_tag(plugin.id, plugin.version)
    try:
        if await exists(tag):
            logger.info("%s: %s (image %s already present)", plugin.id, STATE_INSTALLED, tag)
            return STATE_INSTALLED
        logger.info("%s: %s (%s)", plugin.id, STATE_BUILDING, tag)
        ok = await build(plugin, tag)
    except Exception:  # noqa: BLE001 — one bad plugin must not stop the runner
        logger.exception("%s: %s — image build raised", plugin.id, STATE_FAILED)
        return STATE_FAILED

    if not ok:
        logger.error("%s: %s — image build failed (%s)", plugin.id, STATE_FAILED, tag)
        return STATE_FAILED
    logger.info("%s: %s (%s)", plugin.id, STATE_INSTALLED, tag)
    return STATE_INSTALLED
