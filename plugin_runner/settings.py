"""Runner configuration."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

#: Constant reported to the API on the runner row. Execution is always a plain
#: subprocess bound to the plugin's venv — there is no sandbox and no other mode
#: (trusted first-party code, decision 2). Kept as a constant so the API's
#: ``isolation_mode`` column keeps a value without a corresponding setting.
ISOLATION_MODE = "subprocess"


def default_cache_root() -> Path:
    """Where venvs, the uv cache, and runner state live by default.

    ``/var/cache/catlico`` in a container/host that grants it (writable), else
    ``~/.cache/catlico`` — zero-config on a macOS dev box. Local disk only:
    venvs must never live on EFS/NFS (decision 3).
    """
    system = Path("/var/cache/catlico")
    try:
        system.mkdir(parents=True, exist_ok=True)
        if os.access(system, os.W_OK):
            return system
    except OSError:
        pass
    home = Path.home() / ".cache" / "catlico"
    try:
        home.mkdir(parents=True, exist_ok=True)
        return home
    except OSError:
        return Path(tempfile.gettempdir()) / "catlico-cache"


class RunnerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PLUGIN_RUNNER_", extra="ignore")

    #: This runner's stable id (matches the runner row created in Catlico).
    runner_id: str = "runner-1"
    name: str = "Catlico Plugin Runner"
    version: str = "0.1.0"

    #: Base URL of the Catlico API (control plane) as reached by the *runner*.
    catlico_api_url: str = "http://localhost:8000"

    #: Shared secret configured identically on the Catlico API and this runner.
    #: It is the whole trust boundary: every internal call sends it as
    #: ``Authorization: Bearer <secret>`` (plus ``X-Runner-Id``), and inbound
    #: event/rescan pushes are HMAC-verified against it.
    shared_secret: str = ""

    #: The URL the Catlico API uses to reach this runner for event pushes.
    #: Self-reported at registration. Empty leaves the API unable to push.
    advertised_url: str = ""

    #: Base URL the *plugin* uses to reach the Catlico runtime API. Empty
    #: (default) reuses ``catlico_api_url``. Override when the plugin subprocess
    #: cannot resolve the runner's own URL.
    plugin_api_url: str = ""

    #: Root directory of provisioned plugins — each subdirectory holding a
    #: ``catlico-plugin.toml`` is one plugin (a full uv project). Underscore-
    #: prefixed dirs are skipped (reserved, e.g. ``_wheelhouse``). Pre-provision
    #: it via a baked image, a Docker volume/EFS mount, or ``plugin-runner install``.
    plugins_dir: str = "/plugins"

    #: Where per-plugin dependency venvs are materialised (local disk, never EFS),
    #: keyed by ``sha256(uv.lock)``. Empty -> ``default_cache_root()/venvs``.
    venvs_dir: str = ""

    #: uv's package cache (shared across plugin syncs). Empty ->
    #: ``default_cache_root()/uv``. A persistent volume here makes cold syncs fast
    #: and enables offline (``UV_OFFLINE=1``) operation.
    uv_cache_dir: str = ""

    #: Dev-only editable SDK override. When set, every venv gets the SDK installed
    #: editable from this checkout after ``uv sync`` (the marker records it, so
    #: flipping it rebuilds the venv). Empty -> the SDK resolves from the plugin's
    #: own lockfile (git pin / index).
    sdk_source: str = ""

    #: Wall-clock cap for a single plugin ``uv sync``.
    uv_sync_timeout_seconds: int = 600

    #: How many plugin venvs may sync concurrently at startup.
    venv_sync_concurrency: int = 4

    #: Where the runner persists its local state. Under ``default_cache_root()`` so
    #: an immutable image keeps working; override with PLUGIN_RUNNER_STATE_FILE.
    state_file: str = ""

    #: Private HTTP server bind.
    host: str = "0.0.0.0"
    port: int = 8090

    heartbeat_interval_seconds: int = 30
    http_timeout: float = 30.0

    def resolved_venvs_dir(self) -> Path:
        return Path(self.venvs_dir) if self.venvs_dir else default_cache_root() / "venvs"

    def resolved_uv_cache_dir(self) -> Path:
        return Path(self.uv_cache_dir) if self.uv_cache_dir else default_cache_root() / "uv"

    def resolved_state_file(self) -> Path:
        return Path(self.state_file) if self.state_file else default_cache_root() / "state.json"


def load_settings() -> RunnerSettings:
    return RunnerSettings()
