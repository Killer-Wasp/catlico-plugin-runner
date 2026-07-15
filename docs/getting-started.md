# Getting started

Run `catlico-plugin-runner` for local development.

For the full multi-service stack, see `DEVELOPMENT.md` in the workspace root. For writing a
plugin, start from the [plugin SDK](https://github.com/Killer-Wasp/catlico-plugin-sdk) and the
[plugins catalog](https://github.com/Killer-Wasp/catlico-plugins) instead — you only need to
touch this repo when working on the runner itself.

## How it works

The runner is a `/plugins` directory of **per-plugin uv projects**. On startup it discovers each
plugin (a subdirectory with a `catlico-plugin.toml`), materialises a **per-plugin venv** keyed by
`sha256(uv.lock)` (`uv sync --frozen --no-dev`), self-registers, and serves the private API. Each
run is a **plain subprocess** bound to that plugin's venv interpreter — **no sandbox** (plugins
are trusted first-party code; see [security.md](security.md)). Venvs are content-addressed and
marker-skipped, so a warm restart with unchanged locks does ~0 work.

## Prerequisites

- **Python 3.14+**, **[uv](https://docs.astral.sh/uv/)** (the runner shells out to `uv` to sync
  plugin venvs — it must be on PATH; the runner refuses to start otherwise)
- A running **catlico-api** (default `http://localhost:8000`)
- A checkout of **`catlico-plugin-sdk` as a sibling directory** (`../catlico-plugin-sdk`) — the
  runner depends on it as a path package
- **No Docker required** for local dev (only `make build` for the production image)

## Setup

```bash
git clone https://github.com/Killer-Wasp/catlico-plugin-runner.git
git clone https://github.com/Killer-Wasp/catlico-plugin-sdk.git   # sibling checkout
cd catlico-plugin-runner

make install    # uv sync, including the dev group
```

### Configure

Put these in `.env` (the runner and API authenticate with **one shared secret**):

```bash
PLUGIN_RUNNER_RUNNER_ID=runner-1
PLUGIN_RUNNER_SHARED_SECRET=<same value as the Catlico API>
PLUGIN_RUNNER_ADVERTISED_URL=http://localhost:8090
PLUGIN_RUNNER_CATLICO_API_URL=http://localhost:8000
PLUGIN_RUNNER_PLUGINS_DIR=/absolute/path/to/catlico-plugins
PLUGIN_RUNNER_SDK_SOURCE=../catlico-plugin-sdk   # dev: editable SDK in each venv
```

Generate a secret with `python -c "import secrets; print(secrets.token_hex(32))"`.
`PLUGIN_RUNNER_ADVERTISED_URL` must be reachable *from the API host*. On startup the runner
self-registers — no token to mint, nothing persisted. Full story: [enrollment.md](enrollment.md).

### Run

```bash
make run    # provision venvs, self-register, heartbeat, serve the private API on :8090
make dev    # same, but auto-restarts when runner or SDK code changes
```

## Providing plugins

Point `PLUGIN_RUNNER_PLUGINS_DIR` (a single root) at a directory; each subdirectory with a
`catlico-plugin.toml` is one plugin. Underscore-prefixed dirs (`_wheelhouse`) are reserved and
skipped. Provision plugins by:

- a **baked image** — the default image `COPY`s the full catalog into `/plugins` and pre-syncs
  every venv (`plugin-runner sync`), so a container starts with zero syncs;
- a **volume / EFS mount** at `/plugins`;
- **`plugin-runner install <source>`** at build stage — a local path (copied) or a git URL
  (cloned with `--ref`), validated to contain a `catlico-plugin.toml`.

There is **no runtime/web install** — install is build-time from reviewed source (see
[security.md](security.md)).

### CLI

| Command | Does |
|---|---|
| `plugin-runner` / `plugin-runner serve` | provision venvs, register, serve (the default) |
| `plugin-runner sync` | discover + provision every venv; exits non-zero on any failure (CI / image build) |
| `plugin-runner install <source> [--ref REF] [--name NAME]` | copy a local dir or clone a git repo into the plugins dir |

## Dependencies, private indexes, wheelhouses, offline

The child `uv sync` **inherits the runner's environment** (there is no runner-specific knob), so
standard uv env vars apply to every plugin sync:

- **Private index:** `UV_DEFAULT_INDEX`, `UV_INDEX_<NAME>_USERNAME` / `UV_INDEX_<NAME>_PASSWORD`,
  `UV_NATIVE_TLS`.
- **Non-published deps:** vendor wheels via a plugin's `[tool.uv.sources] path`, a shared
  `/plugins/_wheelhouse` via `UV_FIND_LINKS`, or git sources.
- **Offline:** a persistent `PLUGIN_RUNNER_UV_CACHE_DIR` volume plus `UV_OFFLINE=1` (or a baked
  image, which needs no index at runtime).

## Configuration

All settings use the `PLUGIN_RUNNER_` prefix (`plugin_runner/settings.py`):

| Env var | Default | Meaning |
|---|---|---|
| `PLUGIN_RUNNER_RUNNER_ID` | `runner-1` | Stable runner id; must match the runner row in Catlico |
| `PLUGIN_RUNNER_NAME` / `PLUGIN_RUNNER_VERSION` | … | Reported at registration |
| `PLUGIN_RUNNER_CATLICO_API_URL` | `http://localhost:8000` | Catlico API base URL, as reached by the **runner** |
| `PLUGIN_RUNNER_PLUGIN_API_URL` | `""` | Catlico API base URL as reached by a **plugin**; empty reuses `CATLICO_API_URL` |
| `PLUGIN_RUNNER_SHARED_SECRET` | `""` | Shared secret authenticating the runner and signing inbound pushes; must match the API |
| `PLUGIN_RUNNER_ADVERTISED_URL` | `""` | URL the API uses to reach this runner for event pushes; self-reported at registration |
| `PLUGIN_RUNNER_PLUGINS_DIR` | `/plugins` | Root directory of provisioned plugins |
| `PLUGIN_RUNNER_VENVS_DIR` | `""` | Per-plugin venv dir (local disk); empty → `<cache root>/venvs` |
| `PLUGIN_RUNNER_UV_CACHE_DIR` | `""` | uv package cache; empty → `<cache root>/uv` |
| `PLUGIN_RUNNER_SDK_SOURCE` | `""` | Dev-only: editable SDK install into every venv from this checkout |
| `PLUGIN_RUNNER_UV_SYNC_TIMEOUT_SECONDS` | `600` | Wall-clock cap for one plugin `uv sync` |
| `PLUGIN_RUNNER_VENV_SYNC_CONCURRENCY` | `4` | How many venvs sync concurrently at startup |
| `PLUGIN_RUNNER_HOST` / `PLUGIN_RUNNER_PORT` | `0.0.0.0` / `8090` | Private API bind |
| `PLUGIN_RUNNER_HEARTBEAT_INTERVAL_SECONDS` | `30` | Heartbeat cadence |
| `PLUGIN_RUNNER_HTTP_TIMEOUT` | `30.0` | HTTP client timeout (seconds) |

The cache root is `/var/cache/catlico` if writable, else `~/.cache/catlico` (zero-config on
macOS). Mount a persistent volume there to keep warm venvs across restarts.

## Tests

```bash
make test              # uv run pytest  (fast unit tests + slow real-uv tests)
uv run pytest -m "not slow"   # skip the real-uv venv/isolation tests
```

The suite needs no Docker. The `@pytest.mark.slow` tests exercise real `uv` venv creation
(including the StackStorm host-site-packages isolation proof) and skip cleanly when uv is
unavailable.

## Building the image

```bash
make build    # docker build with the REPO ROOT as context
```

The context is the workspace root so the sibling `catlico-plugin-sdk` resolves. The default image
bakes the full catalog into `/plugins` and pre-syncs every venv (`plugin-runner sync`). A custom
image extends it: `FROM catlico/plugin-runner`, then
`RUN plugin-runner install <git-url> --ref <ref> && plugin-runner sync`.

## Known gaps

- **`POST /internal/runs/{id}/cancel` is a stub** that always returns `{"cancelled": true}`.
- **`POST /internal/plugins/rescan` is implemented but unwired** — no API proxy route or web
  button calls it; rescan is restart-only for operators in this pass.
- **No resource caps.** A runaway plugin can OOM the host; the timeout kill is the only backstop
  (accepted, trusted-code trade-off — see [security.md](security.md)).
- **Log-tail redaction does not match every transformed secret.** It covers raw + common encoded
  forms; a secret the plugin mangles before printing can pass through. Redaction is a backstop.

## Where to go next

- [enrollment.md](enrollment.md) — the shared-secret model, self-registration, event-push signing, rotation
- [security.md](security.md) — the trust model (no sandbox), endpoint auth, secrets handling
- [`AGENTS.md`](../AGENTS.md) — conventions for contributors and AI agents
