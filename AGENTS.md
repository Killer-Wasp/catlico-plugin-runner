# Catlico Plugin Runner

## Collaboration Principles

- Ask, don't assume. If something is unclear, ask before writing a single line. Never make silent assumptions about intent, architecture, or requirements. When running unattended, pick the most reasonable interpretation, proceed, and record the assumption rather than blocking.
- Implement the simplest solution for simple problems, and better solutions for harder problems. Do not over-engineer or add flexibility that is not needed yet.
- Do not touch unrelated code. Surface bad code or design smells you discover so they can be addressed as separate issues.
- Flag uncertainty explicitly. If unsure, ask before proceeding. When useful, conduct a small, localized, low-risk experiment, then bring the hypothesis and results back for discussion. Confidence without certainty causes more damage than admitting a gap.
- Suggest better approaches when they would improve the work, especially when they have a longer-lasting impact than a tactical change.

## What this is

`catlico-plugin-runner` hosts and executes first-party plugins. It is a low-privilege
control-plane worker: it hosts a `/plugins` directory of per-plugin uv projects, gives each its
own dependency venv (keyed by `sha256(uv.lock)`), and runs every event as a plain subprocess
bound to that venv. The Catlico API tells it what to run; it reports outcomes back over the
internal API. **There is no sandbox** — plugins are trusted first-party code (see security.md).

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
- **Plugins never run in the runner process.** Every run is a separate subprocess bound to the
  plugin's own venv python. Plugin code is never imported into the long-lived runner.

## Stack

**Python 3.14**, **uv** (shelled out to for venv syncs — must be on PATH), **Starlette** (private
API), **httpx**. Depends on the sibling path package `../catlico-plugin-sdk` (editable), so the
Docker build context must be the **repo root**.

## Layout

```
plugin_runner/
  main.py        # entrypoint + CLI (serve/sync/install); uv preflight, provision, register
  server.py      # /internal/* routes (health, plugins, events, rescan, cancel, /metrics)
  client.py      # Catlico internal API client (register, claim, submit)
  registry.py    # discovers /plugins; manifest validation + _ALLOWED_PERMISSIONS + SDK gate
  venvs.py       # per-plugin venv provisioning (sha256(uv.lock) marker skip, gc, ensure_all)
  executor.py    # PluginExecutor / SubprocessExecutor (RunRequest/RunResult, redaction)
  gitclone.py    # hardened clone_source used by `plugin-runner install <git-url>`
  engine.py      # run orchestration (claim -> execute -> report; quarantine)
  settings.py    # PLUGIN_RUNNER_* settings + default_cache_root()
```

## Authentication and self-registration

Auth is **one shared secret** (`PLUGIN_RUNNER_SHARED_SECRET`) configured identically on the
runner and the Catlico API — the whole trust boundary. There is no token exchange, no minted
credential, and nothing persisted to disk.

On startup the runner runs a **`uv` preflight** (refuses to start if uv is unusable — every venv
sync would fail), discovers + provisions plugin venvs, then constructs its API client directly
from the shared secret and runner id and calls `register()` **once** — reporting its
`PLUGIN_RUNNER_ADVERTISED_URL` (the URL the API uses to reach it for event pushes), a constant
`isolation_mode = "subprocess"`, and its plugin manifests. There is no token to spend, no
credential cache, and no re-enrollment recovery.

Every runner→API request carries:

```
Authorization: Bearer <shared_secret>
X-Runner-Id: <runner_id>
```

> **Rotation.** Change `PLUGIN_RUNNER_SHARED_SECRET` on the API and every runner to the new
> value and restart both sides together. A wrong secret means every call is simply rejected.
>
> Note: the per-run `runtime_token` (plugin → API) is a separate, unchanged concern — do not
> conflate it with the runner shared secret.

## Private endpoints

Served on `PLUGIN_RUNNER_HOST:PLUGIN_RUNNER_PORT` (default `0.0.0.0:8090`).

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/internal/health` | none | Liveness, `runner_id`, plugin count, `isolation_mode` (`"subprocess"`) |
| GET | `/internal/plugins` | none | Plugins + `status`/`error` + manifests |
| POST | `/internal/events` | **HMAC** | One event envelope; dispatched to matching ready plugins |
| POST | `/internal/plugins/rescan` | **HMAC** | Re-discover + re-sync + `replace_all` (202). Implemented, **unwired** from API/web |
| POST | `/internal/runs/{run_id}/cancel` | none | Stub; returns `{"cancelled": true}` |
| GET | `/metrics` | none | Prometheus text (private-network scrape) |

The signed routes verify `x-catlico-signature: sha256=<hmac-sha256(shared_secret, raw_body)>`
in constant time; a missing/bad signature returns `401`. Unauthenticated routes are guarded only
by the private network.

## Execution model — no sandbox

There is **one** execution adapter: `SubprocessExecutor` (in `executor.py`). Each run is
`python -m catlico_plugin_sdk._worker` run with **the plugin's own venv python**
(`RunRequest.python_executable`) in its own process **group** (`start_new_session=True`); on
timeout the group is `SIGKILL`ed. The environment is inherited (plugins may need PATH/awscli/AWS
creds). Plugins are trusted first-party code — no container, no resource caps. The executor
boundary is kept clean (plugin code never imported into the runner) so a container/bwrap adapter
could slot in later. The worker writes its result JSON to a `result_path` temp file.

**Per-plugin venvs** (`venvs.py`): keyed by `sha256(uv.lock)` (`venv_dir_name = <id>-<sha12>`);
a `.catlico-venv-ok` marker records `{lock_sha256, sdk_source}` and is written only on full
success. Matching marker → **zero uv calls** (warm start). Else `uv sync --frozen --no-dev` with
`UV_PROJECT_ENVIRONMENT`/`UV_CACHE_DIR` overlaid on `os.environ`. Failures keep the markerless
partial dir (self-repair) and quarantine the plugin, never crash the runner. `gc_stale` runs at
**startup only** (never on rescan). `run_uv` is injectable for tests.

## Timeouts, logs, secrets

- **Timeout**: manifest `timeout_seconds` (default 60). On expiry the process group is
  `SIGKILL`ed; recorded as `status = timeout`, `error_kind = timeout`.
- **Log tail** truncated to the last 64 KB (`LOG_TAIL_MAX_BYTES`).
- **Secrets** are fetched per-run from the API. The log tail is **secret-redacted before
  truncation** (`executor._redact`): run-secret values and the run token become `***REDACTED***`
  on every terminal path, in raw + common encoded forms. Backstop only — transformed secrets can
  pass through; plugins must not print secrets.

## Configuration

`PLUGIN_RUNNER_` prefix (`settings.py`): `RUNNER_ID`, `NAME`, `VERSION`, `CATLICO_API_URL`,
`PLUGIN_API_URL`, `SHARED_SECRET`, `ADVERTISED_URL`, `PLUGINS_DIR` (default `/plugins`),
`VENVS_DIR`, `UV_CACHE_DIR`, `SDK_SOURCE` (dev editable SDK), `UV_SYNC_TIMEOUT_SECONDS`,
`VENV_SYNC_CONCURRENCY`, `HOST`, `PORT`, `HEARTBEAT_INTERVAL_SECONDS`, `HTTP_TIMEOUT`. Internal
index / wheelhouse settings are plain uv env passthrough (`UV_DEFAULT_INDEX`, `UV_INDEX_*`,
`UV_FIND_LINKS`, `UV_NATIVE_TLS`, `UV_OFFLINE`). Full table + registry/wheelhouse notes in
`docs/getting-started.md`.

## CLI

`plugin-runner serve` (default) · `plugin-runner sync` (provision all venvs, non-zero on any
failure) · `plugin-runner install <source> [--ref REF] [--name NAME]` (copy a local dir / clone
a git repo into `PLUGINS_DIR`; build-time/dev only).

## Known gaps — do not assume these work

- **`POST /internal/plugins/rescan` is implemented but unwired** — no API proxy route or web
  button calls it; rescan is restart-only for operators in this pass.
- **No resource caps** — a runaway plugin can OOM the host; timeout kill is the only backstop.
- **Cancel is a stub** that always reports success.

## Development

```bash
make install          # uv sync (incl. dev group; SDK path source is editable)
make run              # provision venvs, self-register, heartbeat, serve on :8090
make dev              # same, auto-restart on runner/SDK changes
make test             # uv run pytest (fast + slow real-uv tests)
uv run pytest -m "not slow"   # skip the real-uv venv/isolation tests
make build            # docker build (context is the repo root; bakes the catalog)
```

Tests need no Docker. `@pytest.mark.slow` tests exercise real `uv` venv creation (including the
StackStorm host-site-packages isolation proof) and skip cleanly when uv is unavailable.

## Related

- `catlico-plugin-sdk/AGENTS.md` — the authoring contract this service executes
- `catlico-plugins/AGENTS.md` — the plugins themselves
- `docs/` — the full reference: `getting-started.md`, `authentication.md` (runner authentication, incl. troubleshooting), `security.md`

When documentation and code disagree, treat the code and tests as the source of truth,
then update the stale doc.
