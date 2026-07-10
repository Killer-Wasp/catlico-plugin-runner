"""Async HTTP client for the Catlico internal plugin-runner API."""
from __future__ import annotations

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
        secret: str = "",
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._secret = secret
        self._timeout = timeout
        self._transport = transport
        #: Captured from enrollment; used to verify API-to-runner push signatures.
        self.push_signing_secret = ""

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._secret}"}

    def _url(self, path: str) -> str:
        return f"{self._base}{_INTERNAL_PREFIX}{path}"

    async def register(self, body: dict) -> dict:
        """POST /register — upsert runner and plugin manifests."""
        async with self._client() as client:
            r = await client.post(self._url("/register"), json=body, headers=self._headers())
            r.raise_for_status()
            return r.json()

    async def enroll(self, body: dict) -> dict:
        """Exchange an enrollment token for machine credentials, then adopt them.

        ``body`` must include ``enrollment_token`` and the runner's reported
        plugin manifests. Captures the runner credential (for all later calls)
        and the push-signing secret (to verify inbound event pushes).
        """
        resp = await self.register(body)
        self._secret = resp["runner_credential"]
        self.push_signing_secret = resp.get("push_signing_secret", "")
        return resp

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
