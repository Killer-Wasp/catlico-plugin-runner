# Security model

The runner exists to execute code you did not write. Everything below is load-bearing —
breaking one rule collapses the isolation model.

## The trust boundary

- **No database access.** The runner never touches Postgres. Every state change — runs, results,
  plugin inventory — goes through the Catlico internal API.
- **No public user tokens.** It authenticates with a single shared secret configured
  identically on the runner and the API. It never holds a browser user's session.
- **Browsers never reach it.** Its HTTP surface is a private `/internal/*` API called only by the
  Catlico API host. **Put it on a private network. Do not expose it publicly.**
- **Plugins never run in the runner process.** Each run executes in a separate process
  (subprocess adapter) or a throwaway container (container adapter). Plugin code is never
  imported into the long-lived runner.

## Endpoint authentication

| Path | Auth |
|---|---|
| `POST /internal/events` | **HMAC** over the raw body |
| `GET /internal/health` | none |
| `GET /internal/plugins` | none |
| `POST /internal/runs/{id}/cancel` | none |

Three of the four routes are unauthenticated. **Network isolation is the only control on them.**
`/internal/plugins` discloses your installed plugin inventory and manifests; `/internal/health`
discloses the runner id and plugin count. Neither exposes secrets, but neither should be
reachable from the internet.

See [enrollment.md](enrollment.md) for the shared-secret model and the HMAC scheme.

## Isolation modes

Selected by `PLUGIN_RUNNER_ISOLATION_MODE`. The shipped default is **`container`**.

> `subprocess` is an explicit opt-in for trusted local development and provides **no
> isolation** — the plugin runs on the host with the runner's own privileges. Any value
> other than `container` or `subprocess` is rejected at startup rather than silently
> falling back. When `container` is selected but the container runtime is unusable, the
> runner **refuses to start**: a runner that started degraded would report healthy and
> then fail every run it claimed.

### `subprocess` — trusted mode

Each run executes `python -m catlico_plugin_sdk._worker` in its own process **group**
(`start_new_session=True`). On timeout the whole group is `SIGKILL`ed.

**The plugin runs on the host with the runner's own privileges.** It can read the runner's
filesystem and environment. Use this only for plugins you wrote or audited. It is a development
adapter.

### `container` — untrusted mode (the default)

Each run is a single-use container built for that plugin, from the image
`catlico-plugin/<plugin_id>:<version>`. The runner builds any missing per-plugin images at
startup (`ensure_images`); a plugin whose image can't be built is logged and skipped, never
fatal.

The runtime is hardcoded to `docker` and the network to `bridge`. Neither is configurable today.

`build_container_command` produces this `docker run` line:

| Flag | Effect |
|---|---|
| `--rm -i` | Single-use container, stdin piped in |
| `--network bridge` | Normal bridge — the plugin must reach the Catlico API. `none` would fully isolate it |
| `--memory <n>m` / `--memory-swap <n>m` | Hard memory cap with **no swap headroom** |
| `--cpus <n>` | CPU cap |
| `--pids-limit 256` | Process-count cap (fork-bomb guard) |
| `--read-only` | Read-only root filesystem |
| `--tmpfs /tmp:rw,size=64m` | The only writable path, 64 MB |
| `--cap-drop ALL` | Drops every Linux capability |
| `--security-opt no-new-privileges` | Blocks privilege escalation |
| `--user 65534:65534` | Runs as `nobody`, never root |

The container runs `python -m catlico_plugin_sdk._worker`. The worker emits its result JSON on
stdout behind a `__CATLICO_RESULT__` sentinel line; the runner splits that from the plugin's log
output.

## Timeouts

`timeout_seconds` comes from the plugin's manifest (default 60). On expiry the run is killed —
process-group `SIGKILL`, or `docker kill <name>` — and recorded as `status = timeout`,
`error_kind = timeout`.

## Secrets

Run config and secrets are fetched **per-run** from the API (`GET /runs/{id}/config`) and passed
to the worker. The API only serves them while the run is `accepted` or `running`.

> **Secrets are redacted from the log tail** before it leaves the sandbox, on every terminal
> path (success, failure, timeout, kill) and in both adapters. Every run-secret value and the
> run token are replaced with `***REDACTED***`. Redaction runs *before* the 64 KB truncation,
> so a secret straddling the cut cannot survive as a partial. Values shorter than
> `MIN_SECRET_LEN` are skipped — redacting `""` or `"1"` would corrupt the log without
> protecting a credential.
>
> This is a backstop, not a licence. Plugins should still avoid printing secrets: **encoded
> forms (base64, URL-encoded) are not matched**, and a secret the plugin transforms before
> printing will pass through.

The log tail is truncated to the **last 64 KB** (`LOG_TAIL_MAX_BYTES`).

## What the runner does not have

- **No `/metrics` endpoint.** No Prometheus instrumentation.
- **No persisted credential.** Auth is the single `PLUGIN_RUNNER_SHARED_SECRET`, held in the
  environment on both the runner and the API. Runner→API calls send it as
  `Authorization: Bearer`; API→runner pushes are HMAC-signed with the same secret. Nothing is
  cached to disk — see [enrollment.md](enrollment.md).
- **No runner-side rotation flow.** To rotate, change `PLUGIN_RUNNER_SHARED_SECRET` on the API
  and every runner to the new value and restart both sides together.

## Reporting a vulnerability

Please do not open a public issue for security problems. Open a
[GitHub security advisory](https://github.com/Killer-Wasp/catlico-plugin-runner/security/advisories/new)
instead.
