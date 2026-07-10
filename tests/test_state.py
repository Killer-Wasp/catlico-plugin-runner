"""Credential persistence across runner restarts.

The enrollment token is one-time. Without persistence, every restart re-spends
an already-consumed token and the API rejects it. These tests pin the on-disk
state format, its atomicity and permissions, and the startup bootstrap that
resumes from a stored credential instead of re-enrolling.

All state files are written under pytest ``tmp_path`` — never the repo.
"""
import json
import stat

import httpx
import pytest

from plugin_runner.main import EnrollmentRequired, bootstrap_client
from plugin_runner.settings import RunnerSettings
from plugin_runner.state import RunnerState, load_state, save_state


class _Registry:
    def all(self):
        return []

    def manifests(self):
        return []


def _settings(tmp_path, **kw) -> RunnerSettings:
    return RunnerSettings(
        catlico_api_url="http://catlico:8000",
        state_file=str(tmp_path / "state.json"),
        **kw,
    )


async def _bootstrap(settings, transport, monkeypatch):
    """Run bootstrap_client with every constructed client on ``transport``."""
    import plugin_runner.main as main

    real = main.PluginRunnerClient

    def factory(*args, **kw):
        kw["transport"] = transport
        return real(*args, **kw)

    monkeypatch.setattr(main, "PluginRunnerClient", factory)
    return await bootstrap_client(settings, _Registry())


# --- state file: format, permissions, atomicity -----------------------------


def test_save_load_roundtrip(tmp_path):
    p = tmp_path / "state.json"
    save_state(p, RunnerState(credential="cpr_cred1", push_signing_secret="cps_push1"))
    assert load_state(p) == RunnerState("cpr_cred1", "cps_push1")


def test_saved_state_is_owner_only(tmp_path):
    p = tmp_path / "state.json"
    save_state(p, RunnerState(credential="cpr_cred1"))
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600, oct(mode)


def test_save_leaves_no_temp_file(tmp_path):
    p = tmp_path / "state.json"
    save_state(p, RunnerState(credential="cpr_cred1"))
    assert sorted(f.name for f in tmp_path.iterdir()) == ["state.json"]


def test_failed_write_preserves_existing_file(tmp_path, monkeypatch):
    """A crash mid-write must not corrupt a previously-valid state file, nor
    leave a stray temp file behind."""
    p = tmp_path / "state.json"
    save_state(p, RunnerState("cpr_good", "cps_good"))

    import plugin_runner.state as state_mod

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(state_mod.json, "dump", boom)
    with pytest.raises(OSError):
        save_state(p, RunnerState("cpr_new", "cps_new"))

    # Original survives untouched; the temp file is cleaned up.
    assert load_state(p) == RunnerState("cpr_good", "cps_good")
    assert sorted(f.name for f in tmp_path.iterdir()) == ["state.json"]


# --- state file: absence / corruption ---------------------------------------


def test_missing_state_reads_as_absent(tmp_path):
    assert load_state(tmp_path / "nope.json") is None


def test_corrupt_state_reads_as_absent(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_state(bad) is None


def test_empty_state_reads_as_absent(tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text("")
    assert load_state(empty) is None


def test_state_missing_credential_reads_as_absent(tmp_path):
    p = tmp_path / "state.json"
    p.write_text(json.dumps({"push_signing_secret": "cps_x"}))
    assert load_state(p) is None


# --- bootstrap: enroll / resume / precedence --------------------------------


async def test_first_start_enrolls_and_persists(tmp_path, monkeypatch):
    """No saved state + token → enrolls, then persists the returned secrets."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(
            200,
            json={"runner_credential": "cpr_xyz", "push_signing_secret": "cps_abc"},
        )

    settings = _settings(tmp_path, enrollment_token="tok")
    client = await _bootstrap(settings, httpx.MockTransport(handler), monkeypatch)

    assert any(p.endswith("/register") for p in calls)
    assert client._headers()["Authorization"] == "Bearer cpr_xyz"
    assert client.push_signing_secret == "cps_abc"
    # Persisted to disk for the next start.
    assert load_state(settings.state_file) == RunnerState("cpr_xyz", "cps_abc")


async def test_saved_state_resumes_without_re_enrolling(tmp_path, monkeypatch):
    """Saved state present (and accepted) → the token is NOT re-spent, and the
    client is configured with the saved credential and push secret."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"plugins": []})  # /sync validation

    settings = _settings(tmp_path, enrollment_token="tok")
    save_state(settings.state_file, RunnerState("cpr_stored", "cps_stored"))
    client = await _bootstrap(settings, httpx.MockTransport(handler), monkeypatch)

    assert not any(p.endswith("/register") for p in calls), calls
    assert client._headers()["Authorization"] == "Bearer cpr_stored"
    assert client.push_signing_secret == "cps_stored"


async def test_rejected_saved_credential_re_enrolls_with_token(tmp_path, monkeypatch):
    """Saved credential rejected by the API (runner reset to pending) → fall
    back to enrolling with the token, and overwrite the state cleanly."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sync"):
            return httpx.Response(401, json={"detail": "invalid credential"})
        return httpx.Response(
            200,
            json={"runner_credential": "cpr_fresh", "push_signing_secret": "cps_fresh"},
        )

    settings = _settings(tmp_path, enrollment_token="tok")
    save_state(settings.state_file, RunnerState("cpr_stale", "cps_stale"))
    client = await _bootstrap(settings, httpx.MockTransport(handler), monkeypatch)

    assert client._headers()["Authorization"] == "Bearer cpr_fresh"
    assert load_state(settings.state_file) == RunnerState("cpr_fresh", "cps_fresh")


async def test_corrupt_state_falls_back_to_enrolling(tmp_path, monkeypatch):
    """A malformed state file must not brick startup: warn, treat as absent,
    and enroll (the token is present here so enrollment succeeds)."""
    settings = _settings(tmp_path, enrollment_token="tok")
    from pathlib import Path

    Path(settings.state_file).write_text("{truncated")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"runner_credential": "cpr_new", "push_signing_secret": "cps_new"},
        )

    client = await _bootstrap(settings, httpx.MockTransport(handler), monkeypatch)
    assert client._headers()["Authorization"] == "Bearer cpr_new"


async def test_transient_error_does_not_discard_credentials(tmp_path, monkeypatch):
    """A 5xx during validation is not proof the credential is dead, and it
    cannot be re-minted — the file must survive and the error propagate."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "down"})

    settings = _settings(tmp_path, enrollment_token="tok")
    save_state(settings.state_file, RunnerState("cpr_stored", "cps_stored"))

    with pytest.raises(httpx.HTTPStatusError):
        await _bootstrap(settings, httpx.MockTransport(handler), monkeypatch)
    assert load_state(settings.state_file) == RunnerState("cpr_stored", "cps_stored")


async def test_no_state_and_no_token_fails_loudly(tmp_path, monkeypatch):
    """No usable credential and no token to obtain one → an actionable error,
    not a spent-token enrollment attempt."""
    settings = _settings(tmp_path, enrollment_token="")
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    with pytest.raises(EnrollmentRequired):
        await _bootstrap(settings, transport, monkeypatch)
