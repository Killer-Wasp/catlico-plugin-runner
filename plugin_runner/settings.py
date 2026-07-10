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
    enrollment_token: str = ""

    #: Directories scanned for locally-provisioned plugins (Docker volume mounts).
    plugin_dirs: list[str] = []

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
