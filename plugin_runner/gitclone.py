"""Hardened git clone of plugin source.

Used by the ``plugin-runner install <git-url>`` build-time CLI to fetch a plugin
repo into the plugins dir. Every git invocation is an argv list (never
``shell=True``, never string interpolation into a shell), runs strictly
non-interactively (no credential/host-key prompts), and is wall-clock bounded.
Leading-dash values are rejected and every command uses a ``--`` end-of-options
separator, so a crafted URL/ref cannot be parsed as a git flag.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


class GitCloneError(RuntimeError):
    """Raised when cloning plugin source from git or resolving its ref fails."""


#: Wall-clock cap for any single git invocation. A hanging remote, an enormous
#: repo, or a credential prompt must fail rather than block forever.
_GIT_TIMEOUT_SECONDS = 120


def _default_git_run(argv: list[str]) -> subprocess.CompletedProcess:
    """Real subprocess runner used by ``clone_source``. Never ``shell=True``;
    ``argv`` is always a literal list, so untrusted URLs/refs can't reach a shell.

    Runs strictly non-interactively: ``GIT_TERMINAL_PROMPT=0`` and an SSH
    ``BatchMode`` command make an auth-required URL fail fast instead of blocking
    on a prompt, and a hard ``timeout`` caps every call. A breached timeout
    surfaces as ``subprocess.TimeoutExpired``, which ``clone_source`` translates
    to ``GitCloneError``.
    """
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "ssh -oBatchMode=yes -oStrictHostKeyChecking=accept-new",
    }
    return subprocess.run(
        argv, capture_output=True, text=True, env=env, timeout=_GIT_TIMEOUT_SECONDS
    )


def _reject_dashed(label: str, value: str) -> None:
    """Reject a value git would parse as an option. Argv lists already stop *shell*
    injection, but a leading-dash ``source_ref``/``source_url`` still reaches git
    as a flag (e.g. ``--upload-pack=...`` on a clone URL is RCE on local transport
    — CVE-2018-17456 class). The ``--`` end-of-options separator is the primary
    defence; this is a belt-and-suspenders guard on attacker-influenced values."""
    if value.startswith("-"):
        raise GitCloneError(f"{label} may not begin with '-': {value!r}")


def clone_source(
    source_url: str,
    source_ref: str,
    dest: Path,
    *,
    run=_default_git_run,
) -> str:
    """Clone ``source_url`` at ``source_ref`` into ``dest``, returning the resolved
    40-char commit SHA at HEAD.

    Supported ``source_ref`` kinds:
      - branch or tag name: satisfied by the fast path, a shallow
        ``git clone --depth 1 --branch <ref>``.
      - raw commit SHA (full or abbreviated): ``--branch`` cannot target a commit,
        so the shallow clone fails and this falls back to a blobless full clone
        (``--filter=blob:none``) followed by ``git checkout <ref>``.

    ``run`` is injectable so tests can point this at a local ``file://`` checkout
    with no network, or fake failures/timeouts. Raises ``GitCloneError`` with the
    underlying git stderr/stdout on any failure.
    """
    _reject_dashed("source_url", source_url)
    _reject_dashed("source_ref", source_ref)

    def _git(argv):
        try:
            return run(argv)
        except subprocess.TimeoutExpired as exc:
            raise GitCloneError(
                f"git timed out: {' '.join(str(a) for a in argv[:3])}..."
            ) from exc

    shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    shallow = _git(
        ["git", "clone", "--depth", "1", "--branch", source_ref, "--", source_url, str(dest)]
    )
    if shallow.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        full = _git(["git", "clone", "--filter=blob:none", "--", source_url, str(dest)])
        if full.returncode != 0:
            raise GitCloneError(
                f"git clone of {source_url!r} failed: "
                f"{(full.stderr or full.stdout or '').strip()}"
            )
        checkout = _git(["git", "-C", str(dest), "checkout", source_ref, "--"])
        if checkout.returncode != 0:
            shutil.rmtree(dest, ignore_errors=True)
            raise GitCloneError(
                f"git checkout of ref {source_ref!r} in {source_url!r} failed: "
                f"{(checkout.stderr or checkout.stdout or '').strip()}"
            )

    rev = _git(["git", "-C", str(dest), "rev-parse", "HEAD"])
    if rev.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise GitCloneError(f"git rev-parse HEAD failed: {(rev.stderr or '').strip()}")
    sha = rev.stdout.strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        shutil.rmtree(dest, ignore_errors=True)
        raise GitCloneError(f"unexpected output from git rev-parse HEAD: {sha!r}")
    return sha
