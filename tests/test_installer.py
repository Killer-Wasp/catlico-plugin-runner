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
    # Dependencies install from the uv lock, never requirements.txt.
    assert "requirements.txt" not in dockerfile
    assert "uv export --frozen" in dockerfile
    assert "uv pip install --system" in dockerfile
    assert "ghcr.io/astral-sh/uv" in dockerfile


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
        (d / "uv.lock").write_text("")
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
    assert any("uv.lock" in e for e in result.errors)


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
    (repo / "uv.lock").write_text("")
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


def _boom(*_a, **_k):
    raise AssertionError("run must not be reached — value should be rejected first")


def test_clone_source_rejects_leading_dash_ref(tmp_path):
    # A ref like `--upload-pack=...`/`-f`/`--orphan` would reach git as an OPTION;
    # reject it before ever invoking git (run= must not be called).
    with pytest.raises(GitCloneError, match="source_ref"):
        clone_source("file:///x", "--upload-pack=touch /tmp/pwn", tmp_path / "d", run=_boom)


def test_clone_source_rejects_leading_dash_url(tmp_path):
    with pytest.raises(GitCloneError, match="source_url"):
        clone_source("--upload-pack=touch /tmp/pwn", "main", tmp_path / "d", run=_boom)


def test_clone_source_uses_end_of_options_separator(tmp_path):
    # Every git invocation must carry a `--` end-of-options separator so a
    # crafted (non-dash-leading) url/ref still cannot be reparsed as a flag.
    calls: list[list[str]] = []

    def fake_run(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="a" * 40 + "\n", stderr="")

    clone_source("file:///repo", "main", tmp_path / "d", run=fake_run)
    assert calls, "run was never called"
    assert "--" in calls[0]  # the (successful) shallow clone
    # url/ref both appear AFTER the separator, i.e. as operands not options.
    sep = calls[0].index("--")
    assert "file:///repo" in calls[0][sep + 1:]
    assert "main" in calls[0][:sep]  # ref is a --branch value, guarded separately


def test_clone_source_translates_timeout_to_gitcloneerror(tmp_path):
    # An injected run= that raises TimeoutExpired (as the real timeout-bounded
    # runner would) must surface as GitCloneError, not leak TimeoutExpired.
    def timing_out(argv):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=120)

    with pytest.raises(GitCloneError, match="timed out"):
        clone_source("file:///repo", "main", tmp_path / "d", run=timing_out)


def test_clone_source_rejects_garbage_rev_parse_output(tmp_path):
    # rev-parse output that isn't a 40-char hex SHA must not be returned as a
    # commit id; guard it and clean up the dest.
    dest = tmp_path / "d"

    def fake_run(argv):
        if "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout="not-a-sha\n", stderr="")
        dest.mkdir(exist_ok=True)  # pretend a successful shallow clone populated dest
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    with pytest.raises(GitCloneError, match="rev-parse"):
        clone_source("file:///repo", "main", dest, run=fake_run)
    assert not dest.exists()  # garbage result cleaned up, no repo left behind


def test_clone_source_bad_ref_leaves_no_repo_on_disk(tmp_path):
    # A post-full-clone checkout failure (commit-SHA-shaped ref that doesn't
    # exist) must rmtree the fully-cloned dest rather than leave it behind.
    repo, _ = _make_git_repo(tmp_path)
    dest = tmp_path / "dest"
    missing_sha = "0" * 40  # 40-hex so it takes the full-clone+checkout fallback
    with pytest.raises(GitCloneError):
        clone_source(f"file://{repo}", missing_sha, dest)
    assert not dest.exists()
