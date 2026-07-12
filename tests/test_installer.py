"""Install pipeline: manifest validation, Dockerfile generation, lockfile check,
and the validation path of install_local (no Docker)."""
import subprocess
import textwrap
from pathlib import Path

import pytest

from plugin_runner.installer import (
    STATE_CLONING,
    STATE_FAILED,
    STATE_INSTALLED,
    GitCloneError,
    _stage_sdk,
    _unstage_sdk,
    clone_source,
    generate_dockerfile,
    has_lockfile,
    image_tag,
    install_from_source,
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
    assert "python:3.14-slim" in dockerfile  # SDK/plugins require-python >=3.14


def test_generate_dockerfile_installs_staged_sdk():
    plugin = InstalledPlugin(
        id="acme", version="1.0.0", manifest=_GOOD,
        module="acme.plugin", cls="Plugin", path="/p/src",
    )
    dockerfile = generate_dockerfile(plugin, sdk_dir=".catlico-sdk")
    # Installs from the staged copy, not PyPI, and removes it from the image.
    assert "pip install --no-cache-dir catlico-plugin-sdk" not in dockerfile
    assert "pip install --no-cache-dir /plugin/.catlico-sdk" in dockerfile
    assert "rm -rf /plugin/.catlico-sdk" in dockerfile


def test_stage_sdk_copies_checkout_and_unstage_removes_it(tmp_path):
    sdk = tmp_path / "sdk"
    (sdk / "catlico_plugin_sdk").mkdir(parents=True)
    (sdk / "pyproject.toml").write_text("[project]\nname='catlico-plugin-sdk'\n")
    (sdk / ".venv").mkdir()  # excluded by the copy-ignore patterns
    context = tmp_path / "acme"
    context.mkdir()

    staged = _stage_sdk(context, str(sdk))
    assert staged == ".catlico-sdk"
    assert (context / ".catlico-sdk" / "pyproject.toml").is_file()
    assert not (context / ".catlico-sdk" / ".venv").exists()

    _unstage_sdk(context)
    assert not (context / ".catlico-sdk").exists()


def test_stage_sdk_none_when_unset_or_invalid(tmp_path):
    context = tmp_path / "acme"
    context.mkdir()
    assert _stage_sdk(context, "") is None
    # A path without a pyproject.toml is not a usable checkout -> fall back to PyPI.
    (tmp_path / "notsdk").mkdir()
    assert _stage_sdk(context, str(tmp_path / "notsdk")) is None
    assert not (context / ".catlico-sdk").exists()


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


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )


def _make_git_repo(tmp_path: Path) -> tuple[Path, str]:
    """A local git repo (no network) holding a valid, buildable plugin dir at
    its root, with a single commit. Returns (repo_dir, head_sha)."""
    repo = tmp_path / "src_repo"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "catlico-plugin.toml").write_text(textwrap.dedent("""
        id = "acme"
        version = "1.0.0"
        entrypoint = "acme.plugin:Plugin"
        triggers = ["observable.created"]
        permissions = ["read:observable"]
        timeout_seconds = 60
    """))
    (repo / "requirements.txt").write_text("httpx\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    sha = _git("rev-parse", "HEAD", cwd=repo).stdout.strip()
    return repo, sha


def test_clone_source_branch_ref_returns_head_sha_and_lands_files(tmp_path):
    repo, sha = _make_git_repo(tmp_path)
    dest = tmp_path / "dest"
    resolved = clone_source(f"file://{repo}", "main", dest)
    assert resolved == sha
    assert len(resolved) == 40
    assert (dest / "catlico-plugin.toml").is_file()


def test_clone_source_commit_sha_ref_falls_back_to_full_clone(tmp_path):
    # --branch cannot target a raw commit SHA; clone_source must fall back to
    # a full clone + checkout so commit-pinned installs still work.
    repo, sha = _make_git_repo(tmp_path)
    dest = tmp_path / "dest"
    resolved = clone_source(f"file://{repo}", sha, dest)
    assert resolved == sha
    assert (dest / "catlico-plugin.toml").is_file()


def test_clone_source_bad_ref_raises(tmp_path):
    repo, _ = _make_git_repo(tmp_path)
    dest = tmp_path / "dest"
    with pytest.raises(GitCloneError):
        clone_source(f"file://{repo}", "no-such-ref", dest)


def test_clone_source_bad_url_raises(tmp_path):
    dest = tmp_path / "dest"
    with pytest.raises(GitCloneError):
        clone_source(f"file://{tmp_path / 'does-not-exist'}", "main", dest)


async def test_install_from_source_happy_path_emits_cloning_first(tmp_path):
    repo, sha = _make_git_repo(tmp_path)
    dest = tmp_path / "dest"
    states: list[str] = []
    result = await install_from_source(
        f"file://{repo}", "main", dest,
        build=False, on_state=lambda s: _record(states, s),
    )
    assert result.status == STATE_INSTALLED
    assert result.commit_sha == sha
    assert result.plugin.id == "acme"
    assert states[0] == STATE_CLONING  # previously-dead constant, now emitted
    assert states.index(STATE_CLONING) < states.index("validating")
    assert "validating" in states and "installed" in states


async def test_install_from_source_clone_failure_emits_failed_no_crash(tmp_path):
    dest = tmp_path / "dest"
    states: list[str] = []
    result = await install_from_source(
        f"file://{tmp_path / 'nope'}", "main", dest,
        on_state=lambda s: _record(states, s),
    )
    assert result.status == STATE_FAILED
    assert result.plugin is None
    assert states[0] == STATE_CLONING
    assert states[-1] == STATE_FAILED
    assert any("clone failed" in e for e in result.errors)
