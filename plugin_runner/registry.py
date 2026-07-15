"""Installed-plugin registry.

Discovers plugins from a single ``plugins_dir`` root: each subdirectory holding a
``catlico-plugin.toml`` is one plugin (a full uv project rooted there).
Underscore-prefixed dirs are skipped (reserved, e.g. ``_wheelhouse``). The
manifest's ``entrypoint`` (``module:app_object``, e.g. ``main:catlico``) plus the
plugin root is what the executor needs to import and run the plugin's ``Catlico``
app in a child process bound to that plugin's venv.

This module is also the runner-side manifest authority: it validates the manifest
(permissions vocabulary + shape) and gates the plugin's declared ``sdk`` range
against the bundled SDK version, so an SDK-API break or a bad manifest is caught
at load (``status="failed"``) rather than mid-run.
"""
from __future__ import annotations

import hashlib
import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

import catlico_plugin_sdk

logger = logging.getLogger(__name__)

#: Permissions a plugin may declare — kept in lockstep with the SDK's
#: ``manifest.PERMISSIONS`` and the API's runtime enforcement. With no sandbox,
#: the per-run ``run_token`` + manifest permission scope is the only containment
#: boundary, so this validation stays STRICT (OpenCTI prior art).
_ALLOWED_PERMISSIONS = {
    "read:case", "read:alert", "read:observable",
    "write:case", "write:task", "write:observable", "write:observable_enrichment",
    "write:plugin_result",
}

STATUS_READY = "ready"
STATUS_FAILED = "failed"


@dataclass
class InstalledPlugin:
    id: str
    version: str
    manifest: dict
    module: str
    app_object: str
    path: str  # plugin ROOT (main.py lives here; deps come from the venv)
    fingerprint: str = ""
    venv_python: str = ""
    status: str = STATUS_READY
    error: str | None = None

    @property
    def triggers(self) -> list[str]:
        return list(self.manifest.get("triggers", []))

    @property
    def permissions(self) -> list[str]:
        return list(self.manifest.get("permissions", []))

    @property
    def timeout_seconds(self) -> int:
        value = self.manifest.get("timeout_seconds", 60)
        return int(value) if isinstance(value, int) and value > 0 else 60


@dataclass
class Registry:
    plugins: dict[str, InstalledPlugin] = field(default_factory=dict)

    def add(self, plugin: InstalledPlugin) -> None:
        self.plugins[plugin.id] = plugin

    def all(self) -> list[InstalledPlugin]:
        return list(self.plugins.values())

    def get(self, plugin_id: str) -> InstalledPlugin | None:
        return self.plugins.get(plugin_id)

    def for_trigger(self, event_type: str) -> list[InstalledPlugin]:
        """Ready plugins whose triggers include ``event_type`` (quarantined excluded)."""
        return [
            p for p in self.plugins.values()
            if p.status == STATUS_READY and event_type in p.triggers
        ]

    def manifests(self) -> list[dict]:
        return [p.manifest for p in self.plugins.values()]

    def replace_all(self, plugins: list[InstalledPlugin]) -> None:
        """Atomically swap the whole plugin set (used by rescan)."""
        self.plugins = {p.id: p for p in plugins}


def validate_manifest(manifest: dict) -> list[str]:
    """Return manifest validation errors (empty == valid).

    Covers the required top-level fields, a ``module:app_object`` entrypoint, at
    least one trigger, a permission vocabulary check (STRICT), a positive timeout,
    and the ``sdk`` host-compat range against the bundled SDK version.
    """
    errors: list[str] = []
    for required in ("id", "version", "entrypoint"):
        if not manifest.get(required):
            errors.append(f"manifest missing required field: {required}")
    entrypoint = manifest.get("entrypoint", "")
    if entrypoint and ":" not in entrypoint:
        errors.append("entrypoint must be 'module:app_object' (e.g. 'main:catlico')")
    if not manifest.get("triggers"):
        errors.append("manifest must declare at least one trigger")
    bad_perms = set(manifest.get("permissions", [])) - _ALLOWED_PERMISSIONS
    if bad_perms:
        errors.append(f"unknown permissions requested: {sorted(bad_perms)}")
    timeout = manifest.get("timeout_seconds", 60)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout <= 0:
        errors.append("timeout_seconds must be a positive integer")
    errors.extend(_sdk_compat_errors(manifest))
    return errors


def _sdk_compat_errors(manifest: dict) -> list[str]:
    """Gate the manifest's ``sdk`` range against the bundled SDK version.

    Grafana lesson: refuse a plugin whose ``sdk`` range excludes the runner's
    bundled ``catlico_plugin_sdk.__version__``, so an SDK-API break fails loudly at
    load, not mid-run. A missing/blank ``sdk`` is allowed (unpinned); a malformed
    range is a hard error.
    """
    raw = manifest.get("sdk")
    if not raw:
        return []
    try:
        spec = SpecifierSet(str(raw))
    except InvalidSpecifier:
        return [f"manifest sdk range {raw!r} is not a valid version specifier"]
    try:
        current = Version(catlico_plugin_sdk.__version__)
    except InvalidVersion:  # pragma: no cover — SDK version is well-formed
        return []
    # prereleases=True so a dev SDK build (e.g. 0.1.0.dev) still satisfies a range.
    if not spec.contains(current, prereleases=True):
        return [
            f"plugin requires SDK {raw!r} but the runner bundles "
            f"catlico-plugin-sdk {catlico_plugin_sdk.__version__}"
        ]
    return []


def _fingerprint(directory: Path) -> str:
    """Content hash of a plugin dir — tamper-evident identity of a local install."""
    h = hashlib.sha256()
    for file in sorted(directory.rglob("*")):
        if file.is_file() and "__pycache__" not in file.parts and ".venv" not in file.parts:
            h.update(file.relative_to(directory).as_posix().encode())
            h.update(file.read_bytes())
    return h.hexdigest()


def load_plugin(directory: Path) -> InstalledPlugin:
    """Load one plugin from its root directory, validating the manifest.

    Raises ``ValueError`` only when the manifest can't be parsed or has no
    entrypoint/id (there's no id to register under). Manifest *content* problems
    (bad permission, sdk mismatch, …) are non-fatal: the plugin loads with
    ``status="failed"`` and an ``error`` so it is visible but never dispatched.
    """
    manifest_path = directory / "catlico-plugin.toml"
    with open(manifest_path, "rb") as fh:
        manifest = tomllib.load(fh)
    entrypoint = manifest.get("entrypoint", "")
    if ":" not in entrypoint:
        raise ValueError(f"{manifest_path}: entrypoint must be 'module:app_object'")
    if not manifest.get("id"):
        raise ValueError(f"{manifest_path}: manifest missing required field: id")
    module, app_object = entrypoint.split(":", 1)
    fingerprint = _fingerprint(directory)

    errors = validate_manifest(manifest)
    status = STATUS_FAILED if errors else STATUS_READY
    error = "; ".join(errors) if errors else None

    return InstalledPlugin(
        id=manifest["id"],
        version=manifest.get("version", "0.0.0"),
        manifest={**manifest, "commit_sha": fingerprint},
        module=module,
        app_object=app_object,
        path=str(directory),
        fingerprint=fingerprint,
        status=status,
        error=error,
    )


def discover(plugins_dir: str) -> Registry:
    """Scan the single ``plugins_dir`` root and build a registry.

    Each child dir with a ``catlico-plugin.toml`` is a plugin; ``_``-prefixed dirs
    are reserved and skipped. A dir whose manifest can't even be parsed (no id) is
    logged and skipped — there's nothing to register it under.
    """
    registry = Registry()
    base = Path(plugins_dir)
    if not base.is_dir():
        return registry
    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name.startswith("_"):
            continue
        if not (child / "catlico-plugin.toml").is_file():
            continue
        try:
            registry.add(load_plugin(child))
        except Exception as exc:  # noqa: BLE001 — unparseable/id-less manifest: skip, don't crash
            logger.error("skipping %s: %s", child, exc)
    return registry
