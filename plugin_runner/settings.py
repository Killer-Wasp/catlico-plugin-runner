"""Runner configuration."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class RunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLUGIN_RUNNER_", extra="ignore")

    #: This runner's stable id (matches the runner row created in Catlico).
    runner_id: str = "runner-1"
    name: str = "Catlico Plugin Runner"
    version: str = "0.1.0"

    #: Base URL of the Catlico API (control plane).
    catlico_api_url: str = "http://localhost:8000"

    #: One-time enrollment token, issued by Catlico when the runner is registered.
    #: Only consulted when there is no usable persisted credential (first start,
    #: or recovery after the API rejects a stale credential). See ``state_file``.
    enrollment_token: str = ""

    #: Where the machine credential and push-signing secret from enrollment are
    #: cached so restarts resume instead of re-spending the one-time token. Holds
    #: bearer secrets: created 0600 and written atomically. Must be gitignored.
    state_file: str = ".runner-state.json"

    #: Directories scanned for locally-provisioned plugins (Docker volume mounts).
    plugin_dirs: list[str] = []

    #: Local checkout of ``catlico-plugin-sdk`` to bake into each plugin image.
    #: Needed for local dev, where the SDK is an unpublished sibling checkout that
    #: lives *outside* a plugin's Docker build context — the install pipeline
    #: stages it into the context so the image can ``pip install`` it. Empty
    #: (default) installs the published ``catlico-plugin-sdk`` from PyPI, which is
    #: the production path.
    sdk_source: str = ""

    #: Execution isolation mode (adapter selected by ``main.select_sandbox``).
    #: ``container`` (the default) runs each untrusted third-party plugin in a
    #: throwaway, hardened container — this is the safe default. ``subprocess``
    #: is an explicit opt-in for trusted local development only: it runs plugins
    #: as child processes on the host with no container, no read-only rootfs, no
    #: resource caps and no capability drops — no isolation. Any other value is
    #: rejected at startup (see ``select_sandbox``).
    isolation_mode: str = "container"

    #: Private HTTP server bind.
    host: str = "0.0.0.0"
    port: int = 8090

    heartbeat_interval_seconds: int = 30
    http_timeout: float = 30.0


def load_settings() -> RunnerSettings:
    return RunnerSettings()
