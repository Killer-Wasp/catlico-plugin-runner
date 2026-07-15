"""Hardened git clone: argv-only, leading-dash rejection, ref fallback, timeout."""
import subprocess
from pathlib import Path

import pytest

from plugin_runner.gitclone import GitCloneError, clone_source

_SHA = "a" * 40


def _ok(stdout="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def _fail(stderr="boom") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def test_shallow_clone_fast_path_returns_sha(tmp_path):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[:2] == ["git", "clone"]:
            return _ok()
        if "rev-parse" in argv:
            return _ok(stdout=_SHA + "\n")
        return _ok()

    sha = clone_source("https://x/acme.git", "main", tmp_path / "dest", run=run)
    assert sha == _SHA
    # Fast path: a single shallow clone (no fallback full clone).
    clone_calls = [c for c in calls if c[:2] == ["git", "clone"]]
    assert len(clone_calls) == 1
    assert "--depth" in clone_calls[0]


def test_commit_sha_ref_falls_back_to_full_clone_then_checkout(tmp_path):
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[:2] == ["git", "clone"] and "--depth" in argv:
            return _fail("not a branch")  # shallow --branch <sha> fails
        if argv[:2] == ["git", "clone"]:
            return _ok()  # blobless full clone
        if "checkout" in argv:
            return _ok()
        if "rev-parse" in argv:
            return _ok(stdout=_SHA + "\n")
        return _ok()

    sha = clone_source("https://x/acme.git", "deadbeef", tmp_path / "dest", run=run)
    assert sha == _SHA
    assert any("--filter=blob:none" in c for c in calls)
    assert any("checkout" in c for c in calls)


def test_rejects_leading_dash_url(tmp_path):
    with pytest.raises(GitCloneError, match="may not begin with"):
        clone_source("--upload-pack=evil", "main", tmp_path / "d", run=lambda a: _ok())


def test_rejects_leading_dash_ref(tmp_path):
    with pytest.raises(GitCloneError, match="may not begin with"):
        clone_source("https://x/acme.git", "--orphan", tmp_path / "d", run=lambda a: _ok())


def test_clone_failure_raises(tmp_path):
    def run(argv):
        return _fail("fatal: repository not found")

    with pytest.raises(GitCloneError, match="repository not found"):
        clone_source("https://x/none.git", "main", tmp_path / "d", run=run)


def test_timeout_translates_to_git_clone_error(tmp_path):
    def run(argv):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=120)

    with pytest.raises(GitCloneError, match="timed out"):
        clone_source("https://x/slow.git", "main", tmp_path / "d", run=run)


def test_unexpected_rev_parse_output_raises(tmp_path):
    def run(argv):
        if "rev-parse" in argv:
            return _ok(stdout="not-a-sha\n")
        return _ok()

    with pytest.raises(GitCloneError, match="unexpected output"):
        clone_source("https://x/acme.git", "main", tmp_path / "d", run=run)


def test_never_uses_shell(tmp_path):
    """Every git call must be an argv LIST (never a shell string)."""
    def run(argv):
        assert isinstance(argv, list)
        if "rev-parse" in argv:
            return _ok(stdout=_SHA + "\n")
        return _ok()

    clone_source("https://x/acme.git", "main", tmp_path / "d", run=run)
