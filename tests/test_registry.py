"""Plugin registry: discovery from the plugins dir, manifest validation, SDK gate."""
import textwrap
from pathlib import Path

import catlico_plugin_sdk
from plugin_runner.registry import (
    STATUS_FAILED,
    STATUS_READY,
    InstalledPlugin,
    Registry,
    discover,
    load_plugin,
    validate_manifest,
)

_MANIFEST = """
id = "acme"
name = "Acme"
version = "1.0.0"
sdk = ">=0.1,<1"
entrypoint = "main:catlico"
triggers = ["observable.created", "case.created"]
permissions = ["read:observable"]
timeout_seconds = 45
"""


def _make_plugin_dir(base: Path, name: str = "acme", manifest: str = _MANIFEST) -> Path:
    d = base / name
    (d / "src" / "acme_plugin").mkdir(parents=True)
    (d / "catlico-plugin.toml").write_text(textwrap.dedent(manifest))
    (d / "main.py").write_text("catlico = object()\n")
    return d


def test_load_plugin_parses_manifest(tmp_path):
    _make_plugin_dir(tmp_path)
    plugin = load_plugin(tmp_path / "acme")
    assert plugin.id == "acme"
    assert plugin.version == "1.0.0"
    assert plugin.module == "main"
    assert plugin.app_object == "catlico"
    assert plugin.path == str(tmp_path / "acme")  # plugin ROOT, not src
    assert plugin.triggers == ["observable.created", "case.created"]
    assert plugin.timeout_seconds == 45
    assert plugin.status == STATUS_READY
    assert plugin.fingerprint
    assert plugin.manifest["commit_sha"] == plugin.fingerprint


def test_discover_and_trigger_filter(tmp_path):
    _make_plugin_dir(tmp_path, "acme")
    registry = discover(str(tmp_path))
    assert len(registry.all()) == 1
    assert registry.for_trigger("observable.created")[0].id == "acme"
    assert registry.for_trigger("alert.created") == []


def test_discover_skips_underscore_and_non_plugin_dirs(tmp_path):
    _make_plugin_dir(tmp_path, "acme")
    (tmp_path / "not-a-plugin").mkdir()
    _make_plugin_dir(tmp_path, "_wheelhouse")  # reserved -> skipped
    registry = discover(str(tmp_path))
    assert {p.id for p in registry.all()} == {"acme"}


def test_for_trigger_excludes_quarantined(tmp_path):
    _make_plugin_dir(tmp_path, "acme")
    registry = discover(str(tmp_path))
    registry.all()[0].status = STATUS_FAILED
    assert registry.for_trigger("observable.created") == []


def test_bad_permission_quarantines_plugin(tmp_path):
    bad = _MANIFEST.replace(
        'permissions = ["read:observable"]', 'permissions = ["delete:everything"]'
    )
    _make_plugin_dir(tmp_path, "acme", manifest=bad)
    plugin = load_plugin(tmp_path / "acme")
    assert plugin.status == STATUS_FAILED
    assert "delete:everything" in plugin.error


def test_sdk_version_gate_quarantines_incompatible_plugin(tmp_path):
    # A range that excludes the bundled SDK version must fail at load (Grafana lesson).
    incompatible = _MANIFEST.replace('sdk = ">=0.1,<1"', 'sdk = ">=99,<100"')
    _make_plugin_dir(tmp_path, "acme", manifest=incompatible)
    plugin = load_plugin(tmp_path / "acme")
    assert plugin.status == STATUS_FAILED
    assert catlico_plugin_sdk.__version__ in plugin.error


def test_sdk_gate_passes_for_bundled_version():
    manifest = {
        "id": "x", "version": "1", "entrypoint": "main:catlico",
        "triggers": ["t"], "sdk": f"=={catlico_plugin_sdk.__version__}",
    }
    assert validate_manifest(manifest) == []


def test_replace_all_atomic_swap():
    registry = Registry()
    registry.add(
        InstalledPlugin(
            id="old", version="1", manifest={}, module="main", app_object="catlico", path="/p"
        )
    )
    registry.replace_all(
        [
            InstalledPlugin(
                id="new", version="1", manifest={}, module="main", app_object="catlico", path="/p"
            )
        ]
    )
    assert {p.id for p in registry.all()} == {"new"}


def test_discover_missing_dir_is_empty(tmp_path):
    assert discover(str(tmp_path / "nope")).all() == []
