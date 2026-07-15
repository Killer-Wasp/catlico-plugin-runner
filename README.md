# Catlico Plugin Runner

**The execution host for [Catlico](https://github.com/jimmyruann/catlico-backend) plugins —
runs trusted first-party plugin code without ever holding database credentials.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/)

The runner is a low-privilege control-plane worker. It hosts a `/plugins` directory of per-plugin
uv projects, gives each its own dependency venv (keyed by `sha256(uv.lock)`), and runs every
event as a plain subprocess bound to that venv. The Catlico API tells it what to run; it reports
outcomes back over an internal HTTP API.

```
Catlico API ──signed event push──►  runner
runner      ──claims the run────►  Catlico API
runner      ──executes plugin───►  subprocess (plugin's own venv python)
runner      ──posts result──────►  Catlico API
```

## Execution model — no sandbox, by design

Plugins are **trusted first-party code** that may legitimately need full system access (awscli,
tooling on PATH), so a run is a plain subprocess that inherits the runner's environment — no
container, no resource caps. The subprocess still gives crash isolation, a timeout group-kill,
and secret-redacted log tails, and the plugin runs on its **own venv** interpreter so its
dependencies never leak from the runner (the StackStorm host-fallback bug is impossible here).
Install is **build-time from reviewed source** — there is no runtime/web install path. Container
isolation is a clean future upgrade for untrusted plugins (the executor boundary is kept intact).
Full trust model: **[docs/security.md](docs/security.md)**.

## The trust boundary

- **No database access.** Every state change goes through the Catlico internal API.
- **No user tokens.** One shared secret authenticates the runner and the API to each other.
- **Browsers never reach it.** Its `/internal/*` surface is for the Catlico API host only —
  deploy it on a private network.
- **Plugin code is never imported into the runner process.**

## Quick start

Requires **Python 3.14+**, **[uv](https://docs.astral.sh/uv/)** (on PATH), a running Catlico API,
and a sibling checkout of `catlico-plugin-sdk`.

```bash
git clone https://github.com/Killer-Wasp/catlico-plugin-runner.git
git clone https://github.com/Killer-Wasp/catlico-plugin-sdk.git
cd catlico-plugin-runner

make install
# set PLUGIN_RUNNER_SHARED_SECRET (same value as the API), PLUGIN_RUNNER_ADVERTISED_URL,
# and PLUGIN_RUNNER_PLUGINS_DIR in .env (see docs/getting-started.md), then:
make run       # provisions venvs, self-registers, serves the private API on :8090
```

Full walkthrough: **[docs/getting-started.md](docs/getting-started.md)**.

## CLI

| Command | Does |
|---|---|
| `plugin-runner serve` *(default)* | provision venvs, self-register, serve |
| `plugin-runner sync` | discover + provision every venv; non-zero exit on any failure (CI/build) |
| `plugin-runner install <source> [--ref REF] [--name NAME]` | copy a local dir / clone a git repo into the plugins dir |

## End-to-end check

[`e2e/`](e2e/README.md) is a reusable, plugin-agnostic end-to-end test: it feeds an observable to
a plugin, lets it run through the runner, and asserts the `PluginResult` that comes back.

```bash
./e2e/start_runner.sh observable-validator    # scoped runner (per-plugin venv, no sandbox)
python e2e/e2e_check.py observable-validator  # → ✓ PASS
```

## Documentation

| Doc | What's in it |
|---|---|
| [Getting started](docs/getting-started.md) | Setup, config table, providing plugins, the CLI, registries/wheelhouses, known gaps |
| [Runner authentication](docs/enrollment.md) | Shared-secret model, self-registration, event-push signing, rotation |
| [Security model](docs/security.md) | The trust model (no sandbox), endpoint auth, secrets handling |
| [End-to-end check](e2e/README.md) | Drive any plugin through the runner and assert its result |

Contributors and AI agents: [`AGENTS.md`](AGENTS.md).

## Related repositories

| Repo | Role |
|---|---|
| [catlico-backend](https://github.com/jimmyruann/catlico-backend) | The API — the control plane this runner reports to |
| [catlico-plugin-sdk](https://github.com/Killer-Wasp/catlico-plugin-sdk) | The authoring contract; also provides the worker entrypoint |
| [catlico-plugins](https://github.com/Killer-Wasp/catlico-plugins) | The plugin catalog |

## Status

Working: shared-secret auth with self-registration, heartbeat, signed event push, per-plugin
venv provisioning (marker-skipped warm starts, startup GC), subprocess execution with timeout
kill and secret-redacted log tails, quarantine of broken plugins, `sync`/`install` CLI, result
submission. Implemented but unwired: `POST /internal/plugins/rescan` (no API/web caller yet).
Stubs: run cancellation. See [known gaps](docs/getting-started.md#known-gaps).

## License

[MIT License](LICENSE).
