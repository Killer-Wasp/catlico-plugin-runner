"""Plugin registry: discovery from mounted directories."""
import textwrap
from pathlib import Path

from plugin_runner.registry import discover, load_plugin

_MANIFEST = """
id = "acme"
name = "Acme"
version = "1.0.0"
entrypoint = "acme.plugin:Plugin"
triggers = ["observable.created", "case.created"]
permissions = ["read:observable"]
timeout_seconds = 45
"""


def _make_plugin_dir(base: Path, name: str = "acme") -> Path:
    d = base / name
    (d / "src" / "acme").mkdir(parents=True)
    (d / "catlico-plugin.toml").write_text(textwrap.dedent(_MANIFEST))
    (d / "src" / "acme" / "plugin.py").write_text("class Plugin: ...\n")
    return d


def test_load_plugin_parses_manifest(tmp_path):
    _make_plugin_dir(tmp_path)
    plugin = load_plugin(tmp_path / "acme")
    assert plugin.id == "acme"
    assert plugin.version == "1.0.0"
    assert plugin.module == "acme.plugin"
    assert plugin.cls == "Plugin"
    assert plugin.path.endswith("/src")
    assert plugin.triggers == ["observable.created", "case.created"]
    assert plugin.timeout_seconds == 45
    assert plugin.fingerprint  # content hash present
    assert plugin.manifest["commit_sha"] == plugin.fingerprint


def test_discover_and_trigger_filter(tmp_path):
    _make_plugin_dir(tmp_path, "acme")
    registry = discover([str(tmp_path)])
    assert len(registry.all()) == 1
    assert registry.for_trigger("observable.created")[0].id == "acme"
    assert registry.for_trigger("alert.created") == []


def test_discover_ignores_non_plugin_dirs(tmp_path):
    (tmp_path / "not-a-plugin").mkdir()
    assert discover([str(tmp_path)]).all() == []
