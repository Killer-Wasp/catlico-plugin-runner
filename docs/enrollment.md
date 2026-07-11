# Enrollment and credentials

The runner ships with **no credential**. It obtains one through a one-time token exchange with
the Catlico API.

## The exchange

1. **An admin creates the runner** in Catlico:
   `POST /api/v1/plugin-runners` with an `id`, `name`, and the runner's `base_url`.
   This mints a **one-time enrollment token** and sets `enrollment_state = pending`. The token
   expires after `PLUGIN_RUNNER_ENROLLMENT_TOKEN_TTL_SECONDS` (default 900s, set on the API).

2. **An operator configures the runner** with that token:

   ```bash
   PLUGIN_RUNNER_ENROLLMENT_TOKEN=<token from the admin>
   PLUGIN_RUNNER_RUNNER_ID=<must match the id the admin used>
   PLUGIN_RUNNER_CATLICO_API_URL=https://catlico.example.com
   ```

3. **The runner registers on startup**, POSTing to
   `/api/internal/plugin-runner/register` with the enrollment token and its installed plugin
   manifests.

4. **The API validates and responds.** The token must belong to a `pending` runner, match the
   stored hash, and be unexpired. On success the API returns two secrets:

   | Secret | Prefix | Purpose |
   |---|---|---|
   | `runner_credential` | `cpr_` | Long-lived machine credential. Sent as `Authorization: Bearer` on every later call. The API stores only its SHA-256 hash and requires `enrollment_state == "enrolled"`. |
   | `push_signing_secret` | `cps_` | Per-runner HMAC key. The runner uses it to verify inbound event pushes from the API. |

   The API then **consumes the token** (clears its hash) and flips the runner to `enrolled`.

## Credential persistence (restarts)

> The runner **persists** `runner_credential` and `push_signing_secret` to a state file
> (`PLUGIN_RUNNER_STATE_FILE`, default `.runner-state.json`), created owner-only (`0600`) and
> written atomically. It is gitignored.

The enrollment token is one-time — the API clears it on the first successful `register` — so
the runner resumes from the saved credential rather than re-spending the token. Startup order:

1. If saved state exists, validate the credential against the API (`GET /sync`).
   - Succeeds → **resume; enrollment is skipped entirely.**
   - `401`/`403` → the credential is dead (this is exactly what an admin re-enrollment causes:
     the runner is set back to `pending`). Discard it and enroll with the token, overwriting state.
   - `5xx` / connection error → propagate. A transient API outage must not throw away a
     credential that cannot be re-minted.
2. If there is no usable credential and no token, startup fails with an actionable error
   telling the operator to mint one.

Corrupt, truncated, or partial state warns and falls back to enrolling rather than bricking
startup. **Recovery never requires deleting files by hand:** to rotate, reset the runner to
`pending` in Catlico, supply a fresh token, and restart.

> **Deployment note.** The default state path is relative to the working directory. In a
> container, point `PLUGIN_RUNNER_STATE_FILE` at a persistent volume — otherwise the credential
> survives process restarts but not container recreation.

## Revocation and rotation

There is no runner-side rotation flow. Rotation is driven from the admin API.

To revoke or rotate, an admin **re-creates the runner** (the same `POST`), which:

- sets `enrollment_state` back to `pending`,
- issues a fresh enrollment token.

Because authentication requires `enrolled`, the previously issued `runner_credential` **stops
working immediately**. The runner must re-register with the new token to obtain a new
credential and a new push secret.

## Event-push authentication

Only `/internal/events` is authenticated. The API signs the **raw request body** with the
runner's `push_signing_secret`:

```
x-catlico-signature: sha256=<hex hmac-sha256(push_signing_secret, raw_body)>
```

The runner recomputes the HMAC with the secret captured at enrollment and compares in constant
time. A missing or empty secret, or a bad signature, returns `401`.

`/internal/health` and `/internal/plugins` are **not signed and not authenticated**. The private
network is the only control on them — see [security.md](security.md).

## Troubleshooting

**`Invalid plugin runner enrollment token` on start.** The token is one-time and
unexpired-only. With a healthy state file the runner never re-presents it, so this means the
token was already spent *and* the saved credential is missing or rejected (state file deleted,
or an admin reset the runner to `pending`), or the TTL lapsed before first enrollment. Have an
admin re-create the runner to issue a fresh token, then start.

**Event pushes return 401.** The runner's `push_signing_secret` and the API's stored
secret disagree — typically because the runner re-enrolled and got a new secret while a delivery
was in flight with the old one, or because the runner never completed enrollment. Re-enroll so
both sides share a fresh secret.

**Runner shows offline / unhealthy in Catlico.** The API marks a runner unhealthy when
`POST /api/v1/plugin-runners/{id}/health-check` — which calls the runner's `GET /internal/health`
— can't reach it. Confirm the `base_url` the admin registered is reachable *from the API host*,
and that the process is listening on `PLUGIN_RUNNER_PORT`. Heartbeats also update liveness every
`PLUGIN_RUNNER_HEARTBEAT_INTERVAL_SECONDS`.
