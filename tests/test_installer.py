"""Install pipeline: manifest validation, Dockerfile generation, lockfile check,
and the validation path of install_local (no Docker)."""
import textwrap
from pathlib import Path

from plugin_runner.installer import (
    STATE_FAILED,
    STATE_INSTALLED,
    generate_dockerfile,
    has_lockfile,
    image_tag,
    install_local,
    validate_manifest,
)
from plugin_runner.registry import InstalledPlugin

_GOOD = {
    "id": "acme", "version": "1.0.0", "entrypoint": "acme.plugin:Plugin",
    "triggers": ["observable.created"], "permissions": ["read:observable"],
    "timeout_seconds": 60,
}


def test_validate_accepts_good_manifest():
    assert validate_manifest(_GOOD) == []


def test_validate_flags_problems():
    errors = validate_manifest({
        "id": "acme", "entrypoint": "no-colon",
        "permissions": ["write:everything"], "timeout_seconds": -1,
    })
    joined = " ".join(errors)
    assert "version" in joined
    assert "entrypoint must be" in joined
    assert "at least one trigger" in joined
    assert "unknown permissions" in joined
    assert "timeout_seconds" in joined


def test_image_tag():
    assert image_tag("acme", "1.0.0") == "catlico-plugin/acme:1.0.0"


def test_generate_dockerfile_is_non_root_and_installs_sdk():
    plugin = InstalledPlugin(
        id="acme", version="1.0.0", manifest=_GOOD,
        module="acme.plugin", cls="Plugin", path="/p/src",
    )
    dockerfile = generate_dockerfile(plugin)
    assert "USER 65534:65534" in dockerfile
    assert "pip install --no-cache-dir catlico-plugin-sdk" in dockerfile
    assert "ENTRYPOINT" not in dockerfile  # sandbox sets the command
    assert "python:3.12-slim" in dockerfile


def test_has_lockfile(tmp_path):
    assert not has_lockfile(tmp_path)
    (tmp_path / "uv.lock").write_text("")
    assert has_lockfile(tmp_path)


def _plugin_dir(tmp_path: Path, *, lockfile=True) -> Path:
    d = tmp_path / "acme"
    d.mkdir()
    (d / "catlico-plugin.toml").write_text(textwrap.dedent("""
        id = "acme"
        version = "1.0.0"
        entrypoint = "acme.plugin:Plugin"
        triggers = ["observable.created"]
        permissions = ["read:observable"]
        timeout_seconds = 60
    """))
    if lockfile:
        (d / "requirements.txt").write_text("httpx\n")
    return d


async def test_install_local_validates_without_building(tmp_path):
    states = []
    result = await install_local(
        _plugin_dir(tmp_path), build=False, on_state=lambda s: _record(states, s)
    )
    assert result.status == STATE_INSTALLED
    assert result.plugin.id == "acme"
    assert "validating" in states and "installed" in states


async def test_install_local_strict_requires_lockfile(tmp_path):
    result = await install_local(
        _plugin_dir(tmp_path, lockfile=False), build=False, strict=True
    )
    assert result.status == STATE_FAILED
    assert any("lockfile" in e for e in result.errors)


async def _record(states, s):
    states.append(s)
