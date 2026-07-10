# catlico-plugin-runner

The Catlico plugin runner hosts and executes third-party plugins. It is a
control-plane worker: Catlico (the API) tells it what to run, and it runs each
plugin in an isolated sandbox and reports the outcome back over the internal
API.

## What it is (and its trust boundary)

The runner is deliberately low-privilege:

- **No database access.** It never touches Postgres. Every state change (runs,
  results, plugin inventory) goes through the Catlico internal API.
- **No public user tokens.** It authenticates to the API with a single
  machine credential minted at enrollment. It never holds a browser user's
  session.
- **Browsers never reach it.** Its HTTP surface is a private `/internal/*` API
  called only by the Catlico API host, not by end users. Put it on a private
  network; do not expose it publicly.
- **Plugins never run in the runner process.** Each run executes in a separate
  process (subprocess adapter) or a throwaway container (container adapter).
  Plugin code is never imported into the long-lived runner.

Data flow: Catlico API `→ POST /internal/events` (signed) `→` runner claims a
run from the API `→` runner executes the plugin in a sandbox `→` runner
`→ POST .../result` back to the API.

## Enrollment and credentials

Enrollment is a one-time token exchange. The runner does not ship with a
credential; an operator obtains one from a Catlico admin and hands it to the
runner via the environment.

1. **Admin creates the runner** in Catlico (`POST /api/v1/plugin-runners` with an
   `id`, `name`, and the runner's `base_url`). This mints a **one-time
   enrollment token** and sets the runner to `enrollment_state = pending`. The
   token has a TTL (`PLUGIN_RUNNER_ENROLLMENT_TOKEN_TTL_SECONDS` on the API).
2. **Operator configures the runner** with the token in
   `PLUGIN_RUNNER_ENROLLMENT_TOKEN` (plus `PLUGIN_RUNNER_RUNNER_ID` matching the
   `id` the admin used, and `PLUGIN_RUNNER_CATLICO_API_URL`).
3. **Runner registers on startup**: it `POST`s to
   `/api/internal/plugin-runner/register` with the enrollment token and its
   reported plugin manifests.
4. **API validates and responds.** The token must belong to a `pending` runner,
   match the stored hash, and be unexpired. On success the API returns:
   - `runner_credential` (prefix `cpr_`) — the long-lived machine credential.
     The runner sends it as `Authorization: Bearer <cred>` on every later call.
     The API stores only its SHA-256 hash and requires `enrollment_state ==
     enrolled` to accept it.
   - `push_signing_secret` (prefix `cps_`) — the per-runner HMAC key the runner
     uses to verify inbound event pushes from the API.
   The API then **consumes the token** (clears its hash) and flips the runner to
   `enrolled`.

### Revocation and rotation

- **Revoke / rotate:** an admin re-creates the runner (same `POST`), which sets
  `enrollment_state` back to `pending` and issues a fresh enrollment token.
  Because authentication requires `enrolled`, the previously issued
  `runner_credential` stops working immediately; the runner must re-register
  with the new token to obtain a new credential and push secret.
- There is no runner-side credential-rotation flow; rotation is driven from the
  admin API.

> **Operational caveat (verified against the code):** the runner does **not**
> persist `runner_credential` or `push_signing_secret` to disk. They live only
> in memory. Because the enrollment token is one-time (the API clears it on the
> first successful `register`), **restarting the runner re-attempts enrollment
> with an already-spent token and fails.** A restarted runner needs a freshly
> issued enrollment token. See [Troubleshooting](#troubleshooting).

## Private runner endpoints

The runner serves these on `PLUGIN_RUNNER_HOST:PLUGIN_RUNNER_PORT` (default
`0.0.0.0:8090`). They are internal; only the Catlico API host should reach them.

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/internal/health` | none | Liveness + `runner_id`, installed plugin count, isolation mode. Called by the API's `POST /api/v1/plugin-runners/{id}/health-check`. **Caveat:** `isolation_mode` is hardcoded to `"subprocess"` in the health response (`server.py`) and does not reflect the active adapter — a container-mode runner still reports `"subprocess"`, and the API persists that value onto the runner row (`plugin_runners.py:233`). |
| GET | `/internal/plugins` | none | Reports installed plugins and manifests. Called by the API's `POST /api/v1/plugin-runners/{id}/sync`. |
| POST | `/internal/events` | **HMAC** | Receives one event envelope; dispatches it to matching plugins. Called by the API push loop. |
| POST | `/internal/runs/{run_id}/cancel` | none | Best-effort cancel. Currently a stub that always returns `{"run_id": ..., "cancelled": true}` (inline dispatch usually leaves nothing to kill). |

### HMAC signing scheme

Only `/internal/events` is authenticated. The API signs the **raw request body**
and sends the signature in the `x-catlico-signature` header:

```
x-catlico-signature: sha256=<hex hmac-sha256(push_signing_secret, raw_body)>
```

The runner recomputes the HMAC with the `push_signing_secret` captured at
enrollment and compares in constant time. A missing/empty secret or a bad
signature returns `401`. `health` and `plugins` are **not** signed or
authenticated — the private network is the only control on them.

## Isolation modes

The active adapter is chosen by `PLUGIN_RUNNER_ISOLATION_MODE`:

- **`container` (`ContainerSandboxRunner`) — the default.** Untrusted-mode
  adapter for third-party plugins. Each run is a single-use, hardened container
  built for that plugin. The runtime is hardcoded to `docker` and the network to
  `bridge` (neither is configurable via settings today). Requires a working
  container runtime; the per-plugin images are built at startup from
  `PLUGIN_RUNNER_PLUGIN_DIRS` (see [Install pipeline](#install-pipeline)).
- **`subprocess` (`SubprocessSandboxRunner`) — explicit opt-in, trusted local
  development only.** Each run executes `python -m catlico_plugin_sdk._worker` in
  its own process **group** (`start_new_session=True`); on timeout the whole
  group is `SIGKILL`ed. The plugin runs on the host with the runner's own
  privileges — **no container, read-only rootfs, resource caps or capability
  drops, i.e. no isolation.** Use only for plugins you trust.

Any other value (e.g. a typo like `contianer`) is rejected at startup with an
error naming the valid modes — the runner never silently falls back to an
adapter you did not ask for.

> **Container-runtime preflight.** Because container is the default, a host with
> no working container runtime would go from "runs plugins" to "fails every
> run". At startup, when container isolation is selected, the runner verifies the
> runtime is usable (a `docker version` preflight). If it is not, the runner logs
> a prominent, actionable error and **refuses to start** rather than enroll,
> report healthy, and then fail every claimed run. The error names the explicit
> opt-out (`PLUGIN_RUNNER_ISOLATION_MODE=subprocess`, which disables isolation
> and is for trusted local development only).

### Container hardening

`build_container_command` produces this `docker run` line (every flag verified in
`sandbox.py`):

| Flag | Effect |
|---|---|
| `--rm -i` | Single-use container, stdin piped in |
| `--network bridge` | Normal bridge (plugin must reach the Catlico API); `none` fully isolates |
| `--memory <n>m` / `--memory-swap <n>m` | Hard memory cap with **no swap headroom** |
| `--cpus <n>` | CPU cap |
| `--pids-limit 256` | Process-count cap |
| `--read-only` | Read-only root filesystem |
| `--tmpfs /tmp:rw,size=64m` | Only writable path, 64 MB |
| `--cap-drop ALL` | Drops all Linux capabilities |
| `--security-opt no-new-privileges` | Blocks privilege escalation |
| `--user 65534:65534` | Runs as `nobody`, never root |

The container runs the per-plugin image `catlico-plugin/<plugin_id>:<version>`
(built by the install pipeline) with command `python -m
catlico_plugin_sdk._worker`. The worker emits its result JSON on stdout behind a
`__CATLICO_RESULT__` sentinel line; the runner splits that from the plugin's log
output.

### Timeouts, logs, secrets

- **Timeout:** `timeout_seconds` from the plugin manifest (default 60). On expiry
  the run is killed (process-group `SIGKILL`, or `docker kill <name>`) and the
  result is `status = timeout`, `error_kind = timeout`.
- **Log tail:** stdout/stderr is captured and truncated to the **last 64 KB**
  (`LOG_TAIL_MAX_BYTES`) and stored on the run's `log_tail`.
- **Secrets:** run config and secrets are fetched per-run from the API
  (`GET /runs/{id}/config`) and passed to the worker. The log tail is captured
  raw — there is **no automatic secret redaction** in the current code, so
  plugins must avoid printing secrets.

## Install pipeline

`installer.py` implements a validate → build → health-check state machine.
States surfaced to the web UI: `validating → building → health_checking →
installed`, or `failed`.

- **Manifest validation** (`validate_manifest`): requires `id`, `version`,
  `entrypoint` (`module:Class`), at least one trigger, only known permissions
  (`read:*`/`write:*` from a fixed allow-list), and a positive integer
  `timeout_seconds`.
- **Lockfile check:** looks for `uv.lock`, `poetry.lock`, or `requirements.txt`.
  Missing is a **warning** by default and a **hard error** under `strict=True`.
- **Generated Dockerfile** (`Dockerfile.catlico`): `python:3.12-slim`, creates
  uid `65534`, installs `catlico-plugin-sdk` (+ `requirements.txt` if present),
  runs as `USER 65534:65534`, no `ENTRYPOINT` (the sandbox sets the command).
- **Image build:** `docker build -f Dockerfile.catlico -t
  catlico-plugin/<id>:<version>`.

**Source support:** only **local / Docker-volume** installs work — the pipeline
runs against a plugin directory on disk. **GitHub clone + ref resolution is not
implemented** (a `cloning` state constant exists but is never used, and there is
no clone step). Provision plugins by mounting their directories and pointing
`PLUGIN_RUNNER_PLUGIN_DIRS` at them; the registry discovers any subdirectory
containing a `catlico-plugin.toml`.

> Gap to be aware of: `installer.py` is not wired into the running service today
> (nothing in `main.py`/`server.py` invokes it). At runtime the runner only
> *discovers* already-provisioned plugins; the container adapter therefore
> assumes per-plugin images already exist.

## Configuration

All settings use the `PLUGIN_RUNNER_` env prefix (see `settings.py`).

| Env var | Default | Meaning |
|---|---|---|
| `PLUGIN_RUNNER_RUNNER_ID` | `runner-1` | Stable runner id; must match the runner row created in Catlico |
| `PLUGIN_RUNNER_NAME` | `Catlico Plugin Runner` | Display name reported at registration |
| `PLUGIN_RUNNER_VERSION` | `0.1.0` | Version reported at registration |
| `PLUGIN_RUNNER_CATLICO_API_URL` | `http://localhost:8000` | Catlico API base URL (control plane) |
| `PLUGIN_RUNNER_ENROLLMENT_TOKEN` | `""` | One-time enrollment token from the admin |
| `PLUGIN_RUNNER_PLUGIN_DIRS` | `[]` | Directories scanned for provisioned plugins (JSON list) |
| `PLUGIN_RUNNER_ISOLATION_MODE` | `container` | `container` (untrusted, default; needs a working container runtime) or `subprocess` (trusted local dev, no isolation). Any other value is rejected at startup |
| `PLUGIN_RUNNER_HOST` | `0.0.0.0` | Private API bind host |
| `PLUGIN_RUNNER_PORT` | `8090` | Private API bind port |
| `PLUGIN_RUNNER_HEARTBEAT_INTERVAL_SECONDS` | `30` | Heartbeat cadence to the API |
| `PLUGIN_RUNNER_HTTP_TIMEOUT` | `30.0` | HTTP client timeout (seconds) |

There is **no static shared secret on the runner**: `PLUGIN_RUNNER_SHARED_SECRET`
is not a runner setting. (A same-named, deprecated, unused knob still exists on
the API side — `catlico-api/app/core/configs.py`.) Runner→API auth is the
enrolled `runner_credential`; API→runner event
pushes use the per-runner HMAC `push_signing_secret`. There is **no Prometheus
`/metrics` endpoint.**

## Running it

Prerequisites: [`uv`](https://docs.astral.sh/uv/), and for the container adapter
a container runtime (Docker; Podman-style runtimes work through the same CLI
shape but `docker` is what the code invokes). The runner depends on the sibling
`../catlico-plugin-sdk` path package.

```sh
make install   # uv sync (incl. dev group)
make run        # enroll, start heartbeat loop, serve the private API on :8090
make dev        # like run, auto-restarts on runner/SDK code changes
make test       # uv run pytest
make build      # docker build the runner image (context is the repo root)
```

`make run`/`make dev` source `.env` if present and default
`PLUGIN_RUNNER_CATLICO_API_URL` to `http://localhost:8000`. The Catlico API must
be reachable and the runner must have a valid (unspent) enrollment token before
starting — see [Enrollment](#enrollment-and-credentials).

The container image (`Dockerfile`) is a two-stage `uv` build; its context must be
the **repo root** so the sibling SDK path dependency resolves. It exposes `8090`
and runs the `plugin-runner` entrypoint.

### Tests

`make test` runs `pytest` (asyncio auto mode). The suites are Docker-optional:
container-security enforcement tests are guarded by `skipif(not
shutil.which("docker"))` and skip cleanly when Docker is absent; the pure tests
(command construction, sentinel parsing, manifest validation, Dockerfile
generation, subprocess execution, enrollment) run without Docker.

## Troubleshooting

- **Runner shows offline / unhealthy in Catlico.** The API marks a runner
  unhealthy when `POST .../health-check` (which calls `GET /internal/health`)
  can't reach it. Confirm the runner's `base_url` (registered by the admin) is
  reachable from the API host and that the process is up on
  `PLUGIN_RUNNER_PORT`. Heartbeats also update liveness on
  `PLUGIN_RUNNER_HEARTBEAT_INTERVAL_SECONDS`.
- **Enrollment fails on (re)start** with `Invalid plugin runner enrollment
  token`. The token is one-time and unexpired-only. This happens after the first
  successful enrollment (token already spent), after a runner **restart** (the
  credential is not persisted — see the enrollment caveat), or after the TTL
  lapses. Have an admin re-create the runner to issue a fresh token, then start.
- **Event pushes return 401.** Signature mismatch: the runner's in-memory
  `push_signing_secret` and the API's stored secret disagree — typically because
  the runner re-enrolled (new secret) but the API delivery used a stale one, or
  the runner never completed enrollment. Re-enroll so both sides share a fresh
  secret.
- **Runs end in `timeout`.** The plugin exceeded its manifest `timeout_seconds`.
  The sandbox killed it (process group or `docker kill`). Inspect the run's
  `log_tail` (last 64 KB) and raise the plugin's `timeout_seconds` if the work is
  legitimately long.
- **Install / build failures.** A `failed` install carries `errors` (manifest or
  lockfile problems) or the combined `docker build` log (`image build failed`).
  Fix the manifest/lockfile or the Dockerfile inputs. Note the pipeline is not
  invoked by the running service today (see the install-pipeline gap above).
- **Container runs fail immediately** with `container produced no result`.
  Usually the per-plugin image `catlico-plugin/<id>:<version>` is missing or the
  container runtime is unavailable. Ensure the image exists and `docker` is on
  `PATH`.
