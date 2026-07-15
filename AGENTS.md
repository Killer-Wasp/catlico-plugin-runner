# Catlico Plugin Runner

## Collaboration Principles

- Ask, don't assume. If something is unclear, ask before writing a single line. Never make silent assumptions about intent, architecture, or requirements. When running unattended, pick the most reasonable interpretation, proceed, and record the assumption rather than blocking.
- Implement the simplest solution for simple problems, and better solutions for harder problems. Do not over-engineer or add flexibility that is not needed yet.
- Do not touch unrelated code. Surface bad code or design smells you discover so they can be addressed as separate issues.
- Flag uncertainty explicitly. If unsure, ask before proceeding. When useful, conduct a small, localized, low-risk experiment, then bring the hypothesis and results back for discussion. Confidence without certainty causes more damage than admitting a gap.
- Suggest better approaches when they would improve the work, especially when they have a longer-lasting impact than a tactical change.

## What this is

`catlico-plugin-runner` hosts and executes third-party plugins. It is a low-privilege
control-plane worker: the Catlico API tells it what to run; it runs each plugin in an
isolated sandbox and reports the outcome back over the internal API.

It is the intended successor to `catlico-konnect`, which remains the production
connector worker until the migration completes. Prefer adding new integrations as
**plugins** here, not as konnect connectors. (The replacement plan lives in the private
workspace root's `docs/`; its status snapshot lags the code, so **verify against source.**)

## The trust boundary (the reason this service exists)

Every rule below is load-bearing. Breaking one collapses the isolation model.

- **No database access.** It never touches Postgres. Every state change goes through
  the Catlico internal API.
- **No public user tokens.** It authenticates with one shared secret configured
  identically on the runner and the API. It never holds a browser user's session.
- **Browsers never reach it.** Its `/internal/*` surface is called only by the Catlico
  API host. Put it on a private network; never expose it publicly.
- **Plugins never run in the runner process.** Every run is a separate process or a
  throwaway container. Plugin code is never imported into the long-lived runner.

## Stack

**Python 3.14**, **uv**, **FastAPI** (private API), **httpx**. Depends on the sibling
path package `../catlico-plugin-sdk`, so the Docker build context must be the **repo root**.

## Layout

```
plugin_runner/
  main.py        # entrypoint: self-register, heartbeat loop, serve private API
  server.py      # the four /internal/* routes
  client.py      # Catlico internal API client (register, claim, submit)
  registry.py    # discovers plugin dirs containing catlico-plugin.toml
  sandbox.py     # SubprocessSandboxRunner + ContainerSandboxRunner
  engine.py      # run orchestration
  installer.py   # manifest/lockfile checks + per-plugin image builds (ensure_images runs at startup)
  settings.py    # PLUGIN_RUNNER_* settings
```

## Authentication and self-registration

Auth is **one shared secret** (`PLUGIN_RUNNER_SHARED_SECRET`) configured identically on the
runner and the Catlico API — the whole trust boundary. There is no token exchange, no minted
credential, and nothing persisted to disk.

On startup the runner constructs its API client directly from the shared secret and runner id,
then calls `register()` **once** to self-announce — reporting its `PLUGIN_RUNNER_ADVERTISED_URL`
(the URL the API uses to reach it for event/install pushes) and its installed plugin manifests.
The container-runtime preflight runs *before* registration. There is no token to spend, no
credential cache, and no re-enrollment recovery.

Every runner→API request carries:

```
Authorization: Bearer <shared_secret>
X-Runner-Id: <runner_id>
```

> **Rotation.** Change `PLUGIN_RUNNER_SHARED_SECRET` on the API and every runner to the new
> value and restart both sides together. A wrong secret means every call is simply rejected.
>
> Note: the per-run `runtime_token` (plugin sandbox → API) is a separate, unchanged concern —
> do not conflate it with the runner shared secret.

## Private endpoints

Served on `PLUGIN_RUNNER_HOST:PLUGIN_RUNNER_PORT` (default `0.0.0.0:8090`).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/internal/health` | none | Liveness, `runner_id`, plugin count, isolation mode |
| GET | `/internal/plugins` | none | Installed plugins + manifests |
| POST | `/internal/events` | **HMAC** | One event envelope; dispatched to matching plugins |
| POST | `/internal/runs/{run_id}/cancel` | none | Stub; returns `{"run_id": …, "cancelled": true}` |

Only `/internal/events` is authenticated. The API signs the **raw request body**:

```
x-catlico-signature: sha256=<hex hmac-sha256(shared_secret, raw_body)>
```

The runner recomputes and compares in constant time; a missing/empty secret or bad
signature returns `401`. `health` and `plugins` are unauthenticated — **the private
network is the only control on them.**

`/internal/health` reports the active adapter's real `isolation_mode`.

## Isolation modes

Chosen by `PLUGIN_RUNNER_ISOLATION_MODE`. The shipped default is **`container`**; any
value other than `container`/`subprocess` is rejected at startup (`select_sandbox`) —
never silently mapped to an adapter.

- **`container`** (default) — untrusted-mode adapter. One single-use container per run,
  image `catlico-plugin/<plugin_id>:<version>`; missing images are built at startup by
  `installer.ensure_images` (broken plugins are logged and skipped, never fatal). The
  runner preflights the container runtime and **refuses to start** if it is unusable.
  Runtime is hardcoded to `docker` and the network to `bridge`; neither is configurable.
- **`subprocess`** — trusted-mode dev adapter, explicit opt-in. Runs
  `python -m catlico_plugin_sdk._worker` in its own process **group**
  (`start_new_session=True`); on timeout the group is `SIGKILL`ed. **The plugin runs on
  the host with the runner's own privileges — no isolation.**

Container hardening (`build_container_command`): `--rm -i`, `--network bridge`,
`--memory`/`--memory-swap` (no swap headroom), `--cpus`, `--pids-limit 256`,
`--read-only`, `--tmpfs /tmp:rw,size=64m`, `--cap-drop ALL`,
`--security-opt no-new-privileges`, `--user 65534:65534`.

The worker prints its result JSON on stdout behind a `__CATLICO_RESULT__` sentinel
line; the runner splits that from the plugin's log output.

## Timeouts, logs, secrets

- **Timeout** comes from the plugin manifest's `timeout_seconds` (default 60). On expiry
  the run is killed (process-group `SIGKILL`, or `docker kill`) and recorded as
  `status = timeout`, `error_kind = timeout`.
- **Log tail** is truncated to the last 64 KB (`LOG_TAIL_MAX_BYTES`).
- **Secrets** are fetched per-run from the API and passed to the worker. The log tail is
  **secret-redacted before truncation** (`sandbox._redact`): run-secret values and the run
  token become `***REDACTED***` on every terminal path, in both adapters. It is a backstop —
  encoded/transformed secrets pass through, and values shorter than `MIN_SECRET_LEN` are
  skipped — so plugins still must not print secrets.

## Configuration

All settings use the `PLUGIN_RUNNER_` prefix (`settings.py`): `RUNNER_ID` (default
`runner-1`, stable id for this runner), `NAME`, `VERSION`, `CATLICO_API_URL`,
`SHARED_SECRET` (auth secret; must match the API's value), `ADVERTISED_URL` (URL the API
uses to reach this runner, self-reported at registration), `PLUGIN_DIRS` (JSON list),
`ISOLATION_MODE` (default `container`), `HOST`, `PORT`,
`HEARTBEAT_INTERVAL_SECONDS`, `HTTP_TIMEOUT`.

The runner authenticates with `PLUGIN_RUNNER_SHARED_SECRET`, held in the environment and
never cached to disk. There is **no Prometheus `/metrics` endpoint.**

## Known gaps — do not assume these work

- **GitHub clone install is not implemented.** A `cloning` state constant exists but is
  never used. Only local / Docker-volume installs work: mount plugin directories and
  point `PLUGIN_RUNNER_PLUGIN_DIRS` at them. (`installer.ensure_images` *is* wired —
  `main.py` builds missing per-plugin images at startup.)
- **`/internal/plugins/{id}/resources/{path}` does not exist**, though the API proxies
  to it. Those calls hit a nonexistent route and return the proxy's 502 wrapper.
- **Cancel is a stub** that always reports success.

## Development

```bash
make install   # uv sync (incl. dev group)
make run       # self-register, heartbeat, serve private API on :8090
make dev       # same, auto-restart on runner/SDK changes
make test      # uv run pytest
make build     # docker build (context is the repo root)
```

Tests are Docker-optional: container-security tests are guarded by
`skipif(not shutil.which("docker"))` and skip cleanly; command construction, sentinel
parsing, manifest validation, Dockerfile generation, subprocess execution, and
registration all run without Docker.

## Related

- `catlico-plugin-sdk/AGENTS.md` — the authoring contract this service executes
- `catlico-plugins/AGENTS.md` — the plugins themselves
- `docs/` — the full reference: `getting-started.md`, `enrollment.md` (runner authentication, incl. troubleshooting), `security.md`

When documentation and code disagree, treat the code and tests as the source of truth,
then update the stale doc.
