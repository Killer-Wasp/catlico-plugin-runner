# Getting started

Run `catlico-plugin-runner` for local development.

For the full multi-service stack, see `DEVELOPMENT.md` in the workspace root. For writing a
plugin, start from the [plugin SDK](https://github.com/Killer-Wasp/catlico-plugin-sdk) and the
[plugins catalog](https://github.com/Killer-Wasp/catlico-plugins) instead — you only need to
touch this repo when working on the runner itself.

## Prerequisites

- **Python 3.14+** and **[uv](https://docs.astral.sh/uv/)**
- A running **catlico-api** (default `http://localhost:8000`)
- A checkout of **`catlico-plugin-sdk` as a sibling directory** (`../catlico-plugin-sdk`) —
  the runner depends on it as a path package
- **Docker** — required by the default `container` isolation mode (and `make build`); only the opt-in `subprocess` dev mode runs without it

## Setup

```bash
git clone https://github.com/Killer-Wasp/catlico-plugin-runner.git
git clone https://github.com/Killer-Wasp/catlico-plugin-sdk.git   # sibling checkout
cd catlico-plugin-runner

make install    # uv sync, including the dev group
```

### Configure the shared secret

The runner and the Catlico API authenticate each other with **one shared secret**. Set
`PLUGIN_RUNNER_SHARED_SECRET` on the runner to the same value the API is configured with, and
set `PLUGIN_RUNNER_ADVERTISED_URL` to the URL the API should use to reach this runner. On
startup the runner self-registers — no token to mint, nothing persisted.

Put these in `.env`:

```bash
PLUGIN_RUNNER_RUNNER_ID=runner-1
PLUGIN_RUNNER_SHARED_SECRET=<same value as the Catlico API>
PLUGIN_RUNNER_ADVERTISED_URL=http://localhost:8090
PLUGIN_RUNNER_CATLICO_API_URL=http://localhost:8000
PLUGIN_RUNNER_PLUGIN_DIRS=["/absolute/path/to/catlico-plugins"]
```

Generate a secret with `python -c "import secrets; print(secrets.token_hex(32))"`.
`PLUGIN_RUNNER_ADVERTISED_URL` must be reachable *from the API host*. The full story, including
rotation and troubleshooting, is in [enrollment.md](enrollment.md).

### Run

```bash
make run    # self-register, start the heartbeat loop, serve the private API on :8090
make dev    # same, but auto-restarts when runner or SDK code changes
```

`make run` and `make dev` source `.env` if present and default
`PLUGIN_RUNNER_CATLICO_API_URL` to `http://localhost:8000`.

## Providing plugins

The runner **discovers** plugins; it does not fetch them. Point `PLUGIN_RUNNER_PLUGIN_DIRS`
(a JSON list) at directories to scan — any subdirectory containing a `catlico-plugin.toml`
is registered.

There is no GitHub-clone install: a `cloning` state constant exists in the installer but is
never used. Provision plugins by having them on disk (or Docker-volume-mounted).

## Configuration

All settings use the `PLUGIN_RUNNER_` prefix (`plugin_runner/settings.py`):

| Env var | Default | Meaning |
|---|---|---|
| `PLUGIN_RUNNER_RUNNER_ID` | `runner-1` | Stable runner id; must match the runner row created in Catlico |
| `PLUGIN_RUNNER_NAME` | `Catlico Plugin Runner` | Display name reported at registration |
| `PLUGIN_RUNNER_VERSION` | `0.1.0` | Version reported at registration |
| `PLUGIN_RUNNER_CATLICO_API_URL` | `http://localhost:8000` | Catlico API base URL, as reached by the **runner** |
| `PLUGIN_RUNNER_PLUGIN_API_URL` | `""` | Catlico API base URL as reached by a **plugin sandbox**; empty reuses `CATLICO_API_URL`. Set it when a plugin container can't resolve the runner's URL — e.g. local container-isolation dev on Docker Desktop, where the API is on the host: `http://host.docker.internal:8000` |
| `PLUGIN_RUNNER_SHARED_SECRET` | `""` | Shared secret authenticating the runner to the API and signing inbound event pushes; must match the API's value. Generate with `python -c "import secrets; print(secrets.token_hex(32))"` |
| `PLUGIN_RUNNER_ADVERTISED_URL` | `""` | URL the Catlico API uses to reach this runner for event/install pushes; self-reported at registration and must be reachable from the API host |
| `PLUGIN_RUNNER_PLUGIN_DIRS` | `[]` | Directories scanned for plugins (JSON list) |
| `PLUGIN_RUNNER_ISOLATION_MODE` | `container` | `container` (hardened, the default) or `subprocess` (trusted dev only); anything else is rejected at startup |
| `PLUGIN_RUNNER_CONTAINER_RUNTIME` | `docker` | Container runtime binary the container adapter shells out to (e.g. `podman`); ignored in `subprocess` mode |
| `PLUGIN_RUNNER_CONTAINER_NETWORK` | `bridge` | `--network` value for the container adapter (`bridge`, `none`, or a named network); ignored in `subprocess` mode |
| `PLUGIN_RUNNER_CONTAINER_EXTRA_HOSTS` | `[]` | Extra `--add-host` entries (JSON list of `"name:ip"`) for plugin containers. Empty by default: Docker Desktop resolves `host.docker.internal` natively. On **native Linux** set `["host.docker.internal:host-gateway"]` so a plugin can reach an API on the runner host; ignored in `subprocess` mode |
| `PLUGIN_RUNNER_HOST` | `0.0.0.0` | Private API bind host |
| `PLUGIN_RUNNER_PORT` | `8090` | Private API bind port |
| `PLUGIN_RUNNER_HEARTBEAT_INTERVAL_SECONDS` | `30` | Heartbeat cadence to the API |
| `PLUGIN_RUNNER_HTTP_TIMEOUT` | `30.0` | HTTP client timeout (seconds) |

The two isolation modes and every container hardening flag are documented in
[security.md](security.md). The short version: **`container` is the default** — the runner
preflights the Docker runtime and builds any missing per-plugin images at startup, refusing to
start if the runtime is unusable. `subprocess` is an explicit opt-in for trusted local
development: it runs plugins **on the host with the runner's privileges**, with no isolation.

## Tests

```bash
make test    # uv run pytest
```

The suites are Docker-optional. Container-security enforcement tests are guarded by
`skipif(not shutil.which("docker"))` and skip cleanly when Docker is absent; command
construction, sentinel parsing, manifest validation, Dockerfile generation, subprocess
execution, and registration all run without Docker.

## Building the image

```bash
make build    # docker build with the REPO ROOT as context
```

The context must be the parent directory (the workspace root) so the sibling
`catlico-plugin-sdk` path dependency resolves. The image is a two-stage `uv` build, exposes
`8090`, and runs the `plugin-runner` entrypoint.

## Known gaps

Be aware of these before assuming a feature works:

- **`POST /internal/runs/{id}/cancel` is a stub** that always returns
  `{"run_id": …, "cancelled": true}`.
- **No `/metrics` endpoint.**
- **The container runtime and network are hardcoded** to `docker` and `bridge` — there is no
  setting to select Podman or a different network.
- **Log-tail redaction does not match encoded secrets.** Base64- or URL-encoded forms, or a
  secret the plugin transforms before printing, pass through. Redaction is a backstop.
- **GitHub-clone install is unimplemented.** Plugins are installed by pointing
  `PLUGIN_RUNNER_PLUGIN_DIRS` at a directory on disk; `STATE_CLONING` is defined but never
  emitted.

## Where to go next

- [enrollment.md](enrollment.md) — the shared-secret model, self-registration, event-push signing, rotation
- [security.md](security.md) — trust boundary, isolation modes, container hardening
- [`AGENTS.md`](../AGENTS.md) — conventions for contributors and AI agents
