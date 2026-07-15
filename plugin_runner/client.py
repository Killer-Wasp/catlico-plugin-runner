"""Async HTTP client for the Catlico internal plugin-runner API."""
from __future__ import annotations

import urllib.parse

import httpx

_INTERNAL_PREFIX = "/api/internal/plugin-runner"


class PluginRunnerClient:
    """Talks to the Catlico API's internal plugin-runner endpoints.

    Used by the runner to register, heartbeat, sync manifests, create runs,
    submit lifecycle events, and fetch run-scoped config.
    """

    def __init__(
        self,
        base_url: str,
        shared_secret: str = "",
        runner_id: str = "",
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._secret = shared_secret
        self._runner_id = runner_id
        self._timeout = timeout
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        # The shared secret is the whole trust boundary; every internal call
        # carries it plus the runner's identity header for routing.
        return {
            "Authorization": f"Bearer {self._secret}",
            "X-Runner-Id": self._runner_id,
        }

    def _url(self, path: str) -> str:
        return f"{self._base}{_INTERNAL_PREFIX}{path}"

    async def register(self, body: dict) -> dict:
        """POST /register — self-announce and upsert plugin manifests."""
        async with self._client() as client:
            r = await client.post(self._url("/register"), json=body, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def claim_run(self, body: dict) -> dict:
        """POST /runs — the multi-runner claim. Normalizes the API's responses:

        - 200 queued   -> ``{"outcome": "created", run_id, runtime_token}``
        - 200 skipped  -> ``{"outcome": "skipped", skip_reason}``
        - 409          -> ``{"outcome": "duplicate"}`` (another runner won)
        - 429          -> ``{"outcome": "deferred"}`` (at concurrency cap)
        """
        async with self._client() as client:
            r = await client.post(self._url("/runs"), json=body, headers=self._headers())
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "skipped":
                return {"outcome": "skipped", **data}
            return {"outcome": "created", **data}
        if r.status_code == 409:
            return {"outcome": "duplicate"}
        if r.status_code == 429:
            return {"outcome": "deferred"}
        r.raise_for_status()
        return {"outcome": "error"}

    async def heartbeat(self, body: dict) -> dict:
        """POST /heartbeat — liveness ping."""
        async with self._client() as client:
            r = await client.post(self._url("/heartbeat"), json=body, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def sync(self) -> dict:
        """GET /sync — fetch active plugins and org enablements."""
        async with self._client() as client:
            r = await client.get(self._url("/sync"), headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def create_run(self, body: dict) -> dict:
        """POST /runs — create a PluginRun row."""
        async with self._client() as client:
            r = await client.post(self._url("/runs"), json=body, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def accept_run(self, run_id: str) -> dict:
        """POST /runs/{id}/accepted."""
        async with self._client() as client:
            r = await client.post(
                self._url(f"/runs/{run_id}/accepted"), headers=self._headers()
            )
            r.raise_for_status()
            return r.json()

    async def start_run(self, run_id: str) -> dict:
        """POST /runs/{id}/started."""
        async with self._client() as client:
            r = await client.post(
                self._url(f"/runs/{run_id}/started"), headers=self._headers()
            )
            r.raise_for_status()
            return r.json()

    async def skip_run(self, run_id: str, reason: str) -> dict:
        """POST /runs/{id}/skipped."""
        async with self._client() as client:
            r = await client.post(
                self._url(f"/runs/{run_id}/skipped"),
                json={"skip_reason": reason},
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def submit_result(self, run_id: str, body: dict) -> dict:
        """POST /runs/{id}/result."""
        async with self._client() as client:
            r = await client.post(
                self._url(f"/runs/{run_id}/result"),
                json=body,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json()

    async def report_install_status(
        self,
        plugin_version_id: str,
        state: str,
        *,
        commit_sha: str | None = None,
        image_digest: str | None = None,
        install_log: str | None = None,
        error: str | None = None,
    ) -> dict | None:
        """POST /plugins/{id}/install-status — report install progress back to the
        API sink. ``state`` is one of the installer's ``STATE_*`` wire values
        (cloning/validating/building/health_checking/installed/failed).

        ``plugin_version_id`` may contain ``@``/``/`` (it is a version identifier,
        not a UUID), and ``_url`` builds URLs by plain concatenation, so the id is
        percent-encoded as a single path segment (``safe=""`` also encodes ``/``).
        """
        segment = urllib.parse.quote(plugin_version_id, safe="")
        body = {
            "state": state,
            "commit_sha": commit_sha,
            "image_digest": image_digest,
            "install_log": install_log,
            "error": error,
        }
        async with self._client() as client:
            r = await client.post(
                self._url(f"/plugins/{segment}/install-status"),
                json=body,
                headers=self._headers(),
            )
            r.raise_for_status()
            return r.json() if r.content else None

    async def get_run_config(self, run_id: str) -> dict:
        """GET /runs/{id}/config."""
        async with self._client() as client:
            r = await client.get(
                self._url(f"/runs/{run_id}/config"), headers=self._headers()
            )
            r.raise_for_status()
            return r.json()

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base,
            timeout=self._timeout,
            transport=self._transport,
        )
