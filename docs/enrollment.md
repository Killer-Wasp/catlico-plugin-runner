# Runner authentication (shared secret)

The runner and the Catlico API authenticate each other with **one shared secret**,
configured identically on both sides. There is no token exchange, no minted per-runner
credential, and nothing persisted to disk — the shared secret is the whole trust boundary.

## Configuration

Set the same value on both the API and the runner:

```bash
PLUGIN_RUNNER_SHARED_SECRET=<same value as the Catlico API>
PLUGIN_RUNNER_RUNNER_ID=<stable id for this runner>
PLUGIN_RUNNER_CATLICO_API_URL=https://catlico.example.com
PLUGIN_RUNNER_ADVERTISED_URL=https://runner.internal.example.com:8090
```

Generate a secret with:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

`PLUGIN_RUNNER_ADVERTISED_URL` is the URL the **Catlico API** uses to reach this runner for
event and install pushes. The runner self-reports it at registration, so it must be resolvable
*from the API host*, not just locally.

## Self-registration

On startup the runner constructs its API client directly from the shared secret and runner id,
then calls `register()` **once** to announce itself — reporting its `advertised_url` and its
installed plugin manifests. There is no token to spend, no credential to cache, and no
re-enrollment recovery: if the shared secret is wrong, every call is simply rejected. (The
container-runtime preflight still runs *before* registration.)

Every runner→API request carries:

```
Authorization: Bearer <shared_secret>
X-Runner-Id: <runner_id>
```

## Event-push authentication

Inbound event and install pushes from the API to the runner (`/internal/events`) are signed
with the same shared secret. The API signs the **raw request body**:

```
x-catlico-signature: sha256=<hex hmac-sha256(shared_secret, raw_body)>
```

The runner recomputes the HMAC with its shared secret and compares in constant time. A missing
or empty secret, or a bad signature, returns `401`.

`/internal/health` and `/internal/plugins` are **not signed and not authenticated**. The private
network is the only control on them — see [security.md](security.md).

## Rotation

To rotate, change `PLUGIN_RUNNER_SHARED_SECRET` on the API and on every runner to the new value
and restart. Because both sides read the same secret, they must be updated together.

## Troubleshooting

**Runner→API calls return 401/403.** The runner's `PLUGIN_RUNNER_SHARED_SECRET` does not match
the value the API expects. Confirm both sides hold the identical secret and restart.

**Event pushes return 401.** The runner and API shared secrets disagree, so the HMAC signature
fails to verify. Align the secret on both sides.

**Runner shows offline / unhealthy in Catlico.** The API marks a runner unhealthy when
`POST /api/v1/plugin-runners/{id}/health-check` — which calls the runner's `GET /internal/health`
— can't reach it. Confirm the `advertised_url` the runner reported is reachable *from the API
host*, and that the process is listening on `PLUGIN_RUNNER_PORT`. Heartbeats also update liveness
every `PLUGIN_RUNNER_HEARTBEAT_INTERVAL_SECONDS`.
