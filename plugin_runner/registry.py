"""Installed-plugin registry.

Discovers plugins from mounted directories (Docker volume install): each
subdirectory holding a ``catlico-plugin.toml`` is one plugin. The manifest's
``entrypoint`` (``module:Class``) plus the discovered source path is what the
sandbox needs to import and run the plugin in a child process.
"""
from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class InstalledPlugin:
    id: str
    version: str
    manifest: dict
    module: str
    cls: str
    path: str  # directory to add to sys.path so ``module`` imports
    fingerprint: str = ""

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

    def for_trigger(self, event_type: str) -> list[InstalledPlugin]:
        return [p for p in self.plugins.values() if event_type in p.triggers]

    def manifests(self) -> list[dict]:
        return [p.manifest for p in self.plugins.values()]


def _fingerprint(directory: Path) -> str:
    """Content hash of a plugin dir — the immutable identity of a local install."""
    h = hashlib.sha256()
    for file in sorted(directory.rglob("*")):
        if file.is_file() and "__pycache__" not in file.parts:
            h.update(file.relative_to(directory).as_posix().encode())
            h.update(file.read_bytes())
    return h.hexdigest()


def _source_path(directory: Path) -> str:
    """Where the plugin's importable package lives. Standard layout uses src/."""
    src = directory / "src"
    return str(src if src.is_dir() else directory)


def load_plugin(directory: Path) -> InstalledPlugin:
    manifest_path = directory / "catlico-plugin.toml"
    with open(manifest_path, "rb") as fh:
        manifest = tomllib.load(fh)
    entrypoint = manifest.get("entrypoint", "")
    if ":" not in entrypoint:
        raise ValueError(f"{manifest_path}: entrypoint must be 'module:Class'")
    module, cls = entrypoint.split(":", 1)
    fingerprint = _fingerprint(directory)
    return InstalledPlugin(
        id=manifest["id"],
        version=manifest.get("version", "0.0.0"),
        manifest={**manifest, "commit_sha": fingerprint},
        module=module,
        cls=cls,
        path=_source_path(directory),
        fingerprint=fingerprint,
    )


def discover(dirs: list[str]) -> Registry:
    """Scan directories for plugin projects and build a registry."""
    registry = Registry()
    for d in dirs:
        base = Path(d)
        if not base.is_dir():
            continue
        for child in sorted(base.iterdir()):
            if (child / "catlico-plugin.toml").is_file():
                registry.add(load_plugin(child))
    return registry
