# Catlico Plugin Runner

**The sandbox host for [Catlico](https://github.com/jimmyruann/catlico-backend) plugins —
executes third-party code without ever holding database credentials.**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL_v3-blue.svg)](LICENSE)
[![Python 3.14+](https://img.shields.io/badge/python-3.14+-blue.svg)](https://www.python.org/)

The runner is a low-privilege control-plane worker. The Catlico API tells it what to run; it
executes each plugin in an isolated sandbox — a separate process or a throwaway hardened
container — and reports the outcome back over an internal HTTP API.

```
Catlico API ──signed event push──►  runner
runner      ──claims the run────►  Catlico API
runner      ──executes plugin───►  sandbox (subprocess / single-use container)
runner      ──posts result──────►  Catlico API
```

## The trust boundary

Everything about this service follows from four rules:

- **No database access.** Every state change goes through the Catlico internal API.
- **No user tokens.** It authenticates with one machine credential minted at enrollment.
- **Browsers never reach it.** Its `/internal/*` surface is for the Catlico API host only —
  deploy it on a private network.
- **Plugins never run in the runner process.** Plugin code is never imported into the
  long-lived service.

Details, including the container hardening flags: **[docs/security.md](docs/security.md)**.

## Quick start

Requires **Python 3.14+**, **[uv](https://docs.astral.sh/uv/)**, a running Catlico API, and a
sibling checkout of `catlico-plugin-sdk`.

```bash
git clone https://github.com/Killer-Wasp/catlico-plugin-runner.git
git clone https://github.com/Killer-Wasp/catlico-plugin-sdk.git
cd catlico-plugin-runner

make install
# create the runner in Catlico to obtain a one-time enrollment token,
# put it in .env (see docs/getting-started.md), then:
make run       # serves the private API on :8090
```

Full walkthrough: **[docs/getting-started.md](docs/getting-started.md)**.

> **Restarts work.** The runner persists its machine credential to a gitignored,
> owner-only state file and resumes from it, so a restart does not re-spend the one-time
> enrollment token. If an admin re-enrolls, the stale credential is rejected and the runner
> re-enrolls automatically. See [docs/enrollment.md](docs/enrollment.md).

## Isolation modes

| Mode | Selected by | For |
|---|---|---|
| `container` *(default)* | `PLUGIN_RUNNER_ISOLATION_MODE=container` | Untrusted plugins. One single-use Docker container per run: read-only rootfs, all capabilities dropped, no privilege escalation, memory/CPU/pid caps, runs as `nobody`. Requires a working container runtime — the runner refuses to start without one. |
| `subprocess` | `PLUGIN_RUNNER_ISOLATION_MODE=subprocess` | **Trusted local development only — no isolation.** Runs on the host with the runner's own privileges, own process group, `SIGKILL` on timeout. |

Any other value is rejected at startup with an error naming the valid modes; the runner
never silently falls back to an adapter you did not ask for.

## End-to-end check

[`e2e/`](e2e/README.md) is a reusable, plugin-agnostic end-to-end test: it feeds an
observable to a plugin, lets it run all the way through the runner's container
sandbox, and asserts the `PluginResult` that comes back (the row the web UI shows).

```bash
./e2e/start_runner.sh observable-validator   # scoped runner, container isolation
python e2e/e2e_check.py observable-validator  # → ✓ PASS
```

Point it at any other plugin via `e2e/scenarios.json` or CLI flags — see
[`e2e/README.md`](e2e/README.md).

## Documentation

| Doc | What's in it |
|---|---|
| [Getting started](docs/getting-started.md) | Setup, enrollment, configuration table, providing plugins, known gaps |
| [Enrollment](docs/enrollment.md) | The one-time token exchange, credentials, rotation, the restart trap |
| [Security model](docs/security.md) | Trust boundary, endpoint auth, sandbox hardening, secrets handling |
| [End-to-end check](e2e/README.md) | Reusable e2e: drive any plugin through the runner and assert its result |

Contributors and AI agents: [`AGENTS.md`](AGENTS.md).

## Related repositories

| Repo | Role |
|---|---|
| [catlico-backend](https://github.com/jimmyruann/catlico-backend) | The API — the control plane this runner reports to |
| [catlico-plugin-sdk](https://github.com/Killer-Wasp/catlico-plugin-sdk) | The authoring contract; also provides the sandbox worker entrypoint |
| [catlico-plugins](https://github.com/Killer-Wasp/catlico-plugins) | The plugin catalog |

## Status

Working: enrollment with credential persistence across restarts, heartbeat, signed event
push, subprocess and container sandboxes, timeout kill, secret-redacted log tails, the
validate→build install pipeline (invoked at startup for container mode), result submission.
Not yet wired: run cancellation is a stub; there is no metrics endpoint; the container
runtime and network are hardcoded to `docker`/`bridge`. The gaps are listed honestly in
[docs/getting-started.md](docs/getting-started.md#known-gaps).

## License

[GNU Affero General Public License v3.0](LICENSE).
