"""Enrollment exchange and run-claim normalization."""
import httpx

from plugin_runner.client import PluginRunnerClient


def _client(handler) -> PluginRunnerClient:
    return PluginRunnerClient("http://catlico:8000", transport=httpx.MockTransport(handler))


async def test_enroll_captures_credentials():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/plugin-runner/register")
        body = httpx.Response(200)  # noqa: F841
        return httpx.Response(
            200,
            json={
                "id": "runner-1",
                "runner_credential": "cred-xyz",
                "push_signing_secret": "push-abc",
            },
        )

    client = _client(handler)
    resp = await client.enroll({"id": "runner-1", "enrollment_token": "tok", "plugins": []})
    assert resp["runner_credential"] == "cred-xyz"
    # Adopts the credential for later calls and captures the push secret.
    assert client._headers()["Authorization"] == "Bearer cred-xyz"
    assert client.push_signing_secret == "push-abc"


async def test_claim_run_outcomes():
    cases = {
        "created": httpx.Response(200, json={"status": "queued", "run_id": "r1", "runtime_token": "t1"}),
        "skipped": httpx.Response(200, json={"status": "skipped", "skip_reason": "fresh_result"}),
        "duplicate": httpx.Response(409, json={"detail": "exists"}),
        "deferred": httpx.Response(429, json={"detail": "at cap"}),
    }
    for expected, response in cases.items():
        client = _client(lambda req, r=response: r)
        out = await client.claim_run({"event_id": "e", "plugin_id": "p"})
        assert out["outcome"] == expected
        if expected == "created":
            assert out["run_id"] == "r1"
            assert out["runtime_token"] == "t1"
        if expected == "skipped":
            assert out["skip_reason"] == "fresh_result"
