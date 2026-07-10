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

    #: Default execution isolation. ``subprocess`` is trusted-mode; ``container``
    #: is the untrusted default (adapter selected by the runner).
    isolation_mode: str = "subprocess"

    #: Private HTTP server bind.
    host: str = "0.0.0.0"
    port: int = 8090

    heartbeat_interval_seconds: int = 30
    http_timeout: float = 30.0


def load_settings() -> RunnerSettings:
    return RunnerSettings()
