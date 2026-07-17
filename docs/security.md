# Security model

**The runner executes trusted first-party code with the runner's own privileges. There is no
sandbox.** This is a deliberate design decision (see below), not a gap. The controls here are
about *trust, integrity, and blast radius* — not containment of hostile code.

## The trust model — read this first

- **Plugins run with the runner's privileges.** Each run is a plain subprocess bound to the
  plugin's own venv interpreter. It inherits the runner's environment and can read the runner's
  filesystem, reach the network, and shell out (awscli, external tooling on PATH) — because
  legitimate SOC plugins need to. There are **no resource caps, no read-only rootfs, no dropped
  capabilities, no container**.
- **Therefore: install only code you have reviewed.** The whole security posture rests on this.
  A malicious or compromised plugin is RCE on the runner host. Install is a **build-time** action
  from reviewed source (see below) — never a runtime/web action — precisely so review is a
  mandatory step, not an afterthought (the ComfyUI/Home-Assistant supply-chain postmortems are
  the cautionary tale).
- **Why no sandbox?** Plugins are first-party, reviewed code, and a real sandbox that still lets
  them shell out to arbitrary tooling is not a sandbox. Container/bwrap isolation is a conscious
  *future* upgrade path for third-party plugins — the executor keeps a clean boundary (one
  `PluginExecutor` interface, plugin code is never imported into the runner) so an adapter can
  slot in later without touching the rest of the runner.

What the subprocess model *does* give you — as execution mechanics, not a containment boundary:

- **Crash isolation.** A plugin runs in its own process, never in the long-lived runner; a
  segfault or `sys.exit` can't take the runner down.
- **Timeout kill.** Each run has its own process group (`start_new_session=True`); on
  `timeout_seconds` expiry the whole group is `SIGKILL`ed. This is the *only* backstop against a
  runaway plugin — a plugin that burns CPU/RAM within its timeout can still OOM the host.
- **Dependency isolation.** The plugin runs on its *own venv* interpreter, so its dependencies
  come solely from that venv — the runner's site-packages are never on the plugin's path (the
  StackStorm host-fallback bug is impossible by construction; there is a test that proves it).
- **Secret-redacted log tails.** Protects *stored* logs, below.

## The trust boundary (still enforced)

- **No database access.** The runner never touches Postgres. Every state change goes through the
  Catlico internal API.
- **No public user tokens.** It authenticates with a single shared secret configured identically
  on the runner and the API. It never holds a browser user's session.
- **Per-run scope.** Each run gets a short-lived `run_token` carrying only the plugin's manifest
  permissions; the API rejects any call outside them. With no sandbox, **this per-run token +
  manifest permission scope is the containment boundary** — so permission validation at discovery
  is kept STRICT (a plugin requesting an unknown permission fails to load).
- **Browsers never reach it.** Its HTTP surface is a private `/internal/*` API called only by the
  Catlico API host. **Put it on a private network. Do not expose it publicly.**

## Install is build-time, from reviewed source

Plugins are provisioned into `PLUGIN_RUNNER_PLUGINS_DIR` (default `/plugins`) — a baked image
layer, a volume/EFS mount, or `plugin-runner install <source>` at build stage. **There is no
runtime/web install path.** The default image bakes the full reviewed catalog. A custom image is
`FROM catlico/plugin-runner` + `RUN plugin-runner install <git-url> --ref <ref> && plugin-runner
sync`. Integrity is tamper-evident via the per-plugin directory `commit_sha` fingerprint; a
signed-manifest check is the clean future upgrade (integrity/provenance ≠ sandbox).

## SDK-version gate

Each plugin's manifest declares an `sdk` range. At discovery the runner refuses (quarantines) a
plugin whose range excludes the bundled `catlico_plugin_sdk` version, so an SDK-API break fails
loudly at load, not mid-run.

## Endpoint authentication

| Path | Auth |
|---|---|
| `POST /internal/events` | **HMAC** over the raw body |
| `POST /internal/plugins/rescan` | **HMAC** over the raw body (implemented; unwired from API/web) |
| `GET /internal/health` | none |
| `GET /internal/plugins` | none |
| `POST /internal/runs/{id}/cancel` | none |
| `GET /metrics` | none (Prometheus-conventional) |

The unauthenticated routes disclose the plugin inventory (with status/error), the runner id, and
metrics — no secrets, but **network isolation is the only control on them**. See
[authentication.md](authentication.md) for the shared-secret model and HMAC scheme.

## Timeouts

`timeout_seconds` comes from the plugin's manifest (default 60). On expiry the run's process group
is `SIGKILL`ed and recorded as `status = timeout`, `error_kind = timeout`.

## Secrets

Run config and secrets are fetched **per-run** from the API (`GET /runs/{id}/config`) and passed
to the worker. The API only serves them while the run is `accepted` or `running`.

> **Secrets are redacted from the log tail** on every terminal path (success, failure, timeout,
> kill). Every run-secret value and the run token are replaced with `***REDACTED***`, in raw and
> common encoded forms (base64, URL-encoding). Redaction runs *before* the 64 KB truncation so a
> secret straddling the cut cannot survive as a partial. Values shorter than `MIN_SECRET_LEN` are
> skipped — redacting `""` or `"1"` would corrupt the log without protecting a credential.
>
> This is a backstop, not a licence. Plugins should still never print secrets: a secret the
> plugin transforms before printing can pass through.

## Reporting a vulnerability

Please do not open a public issue for security problems. Open a
[GitHub security advisory](https://github.com/Killer-Wasp/catlico-plugin-runner/security/advisories/new)
instead.
