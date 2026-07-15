# Plugin runner — end-to-end check (reusable)

A repeatable, **plugin-agnostic** end-to-end test: feed an observable to a plugin,
let it run all the way through the runner, and assert the `PluginResult` that comes
back — the exact row the web UI's **Plugin Results** panel displays.

```
create observable → observable.created → API HMAC-push to the runner
→ runner claims a run → plugin executes in a subprocess (its own per-plugin venv)
→ ctx.api writes a PluginResult over the runtime API
→ e2e_check.py polls the result and asserts it
```

It works for **any** plugin. `observable-validator` (offline, deterministic) is the
reference smoke test; the same harness tests vendor plugins like `ip-api` or
`abuseipdb` by changing a scenario.

## Where this fits — the test layers

Testing a plugin does **not** require this full-stack check. Pick the cheapest layer
that proves what you need; `ctx.api` and the runner client are injected as an HTTP
base URL, so a fake substitutes at every level:

| Layer | Command | Stack needed | Proves |
|---|---|---|---|
| Plugin logic (`catlico-plugins/<p>/tests/test_parity.py`) | `uv run pytest` | none | verdict/taxonomy from mocked vendor responses |
| **Executor** (`tests/test_executor.py`) | `uv run pytest` | none | a real `main.py` Catlico app runs in a subprocess via the SDK worker |
| Runner dispatch (`tests/test_engine.py`) | `uv run pytest` | none (mocked client) | claim/dispatch orchestration |
| **Full e2e** (`e2e/e2e_check.py`, this dir) | see below | API + DB + runner | the real dispatch → subprocess → persistence loop |

The executor tests are the "run it for real without the API" answer: they run a real
`Catlico` app through the SDK worker in a subprocess. Use the full e2e below only to
confirm the real control plane.

## Files

| file | purpose |
|---|---|
| `e2e_check.py`   | the reusable checker (stdlib only). `python e2e/e2e_check.py <plugin-id>` |
| `scenarios.json` | per-plugin scenarios: observable to feed, config/secrets, what to assert |
| `start_runner.sh`| start a runner scoped to the plugin(s) under test (per-plugin venv, no sandbox) |
| `README.md`      | this file |

## Prerequisites (one-time per session)

`uv` on PATH (the runner syncs a venv per scoped plugin), plus:

### 1. API — reachable from the runner

```bash
cd catlico-api
set -a; . ./.env; set +a
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 2. Runner — scoped to the plugin(s) under test

```bash
cd catlico-plugin-runner
./e2e/start_runner.sh observable-validator            # one plugin
./e2e/start_runner.sh observable-validator ip-api     # several
```

The runner syncs one venv per scoped plugin, self-registers (shared secret — no
enrollment token), and reports them to the API. Set `PLUGIN_RUNNER_SHARED_SECRET`
(same value as the API) in the runner's `.env` first:

```bash
./e2e/start_runner.sh observable-validator
```

Wait for `provisioned N plugin(s)` and `registered runner …` in the runner log. Adding
a plugin the runner hasn't seen before is just a restart with it in the scoped set (the
runner reports its plugin set on every self-registration).

### 3. Web (optional, for eyeballing) — `cd catlico-web && pnpm dev` → http://localhost:3000

### Networking note

- The plugin subprocess reaches the API at `PLUGIN_RUNNER_PLUGIN_API_URL` (empty →
  reuses `PLUGIN_RUNNER_CATLICO_API_URL`). Since there is no container, `localhost`
  works — no `host.docker.internal` needed.

## Run the check

```bash
python e2e/e2e_check.py observable-validator     # deterministic smoke test
python e2e/e2e_check.py ip-api                   # a live-vendor plugin
```

Expected tail:

```
  ✓ got PluginResult: verdict='info' source='IP-API' summary='IP-API: United States (Google LLC)'
✓ PASS — ip-api produced a result for ip:8.8.8.8 end-to-end through the runner (case #NN).
```

Exit `0` = PASS, `1` = FAIL. Re-run any time; each run creates a fresh case + observable.

Global env: `CATLICO_API_URL` (default `http://localhost:8000`), `CATLICO_ORG`
(`catlico-demo`), `CATLICO_ADMIN_EMAIL` / `CATLICO_ADMIN_PASSWORD`
(`admin@example.com` / `changeme`), `CATLICO_E2E_TIMEOUT` (`60`).

## Test a DIFFERENT plugin

The harness does the same 7 steps for any plugin: authenticate → check the plugin is
available on a healthy runner → set config/secrets → enable + auto-run → create a
case + observable → poll `GET /observables/{id}/plugin-results` → assert. To point it
at another plugin:

**1. Scope a runner to it** (§2 above), e.g. `./e2e/start_runner.sh crtsh`.

**2a. Add a scenario** to `scenarios.json`, keyed by plugin id:

```jsonc
"greynoise": {
  "observable_type": "ip",           // ip | domain | fqdn | url | mail | hash
  "observable_value": "8.8.8.8",     // a value that type accepts
  "settings": {},                    // non-secret config params
  "secrets_env": { "key": "GREYNOISE_API_KEY" },  // secret param -> env var to read
  "expect": { "source": "GreyNoise" } // see "What to assert" below
}
```
then `export GREYNOISE_API_KEY=…` and run `python e2e/e2e_check.py greynoise`.

**2b. Or pass it ad-hoc** (no file edit):

```bash
python e2e/e2e_check.py abuseipdb \
  --type ip --value 118.25.6.39 \
  --secret key=$ABUSEIPDB_API_KEY \
  --expect-source AbuseIPDB
```

CLI flags: `--type`, `--value`, `--setting k=v` (repeatable), `--secret k=v`
(repeatable, literal value), `--expect-verdict`, `--expect-source`. Flags override the
scenario file.

### What to assert

- **Deterministic / offline** plugins (e.g. `observable-validator`): assert the exact
  `verdict` and `normalized` fields — the output is stable.
- **Live-vendor** plugins (e.g. `ip-api`, `abuseipdb`, `greynoise`): assert only
  `source` (the verdict depends on live data). Give a generous `CATLICO_E2E_TIMEOUT`.
- **Secrets** never live in `scenarios.json`; use `secrets_env` (param → env var) or
  `--secret`. Keyed plugins whose `config_complete` is false will fail fast with a
  clear message.
- **Slow vendors**: some services are rate-limited or slow enough to exceed the
  plugin's `timeout_seconds` (crt.sh is a known offender → `error_kind=timeout`). Pick
  a fast vendor for a reliable smoke test; treat slow ones as best-effort.

## Troubleshooting

Check the run rows the API recorded (Postgres in the `catlico-api-db-1` container):

```bash
docker exec catlico-api-db-1 psql -U catlico -d catlico -x -c \
  "select status, error_kind, error, left(log_tail,400) from plugin_run \
   where plugin_id='<id>' order by created_at desc limit 1;"
```

| symptom | cause / fix |
|---|---|
| `plugin … not registered` | runner isn't up, or isn't scoped to this plugin — `start_runner.sh <id>` (re-enroll to register a new one) |
| `available=false` | no healthy runner hosts it — check the runner log / heartbeat |
| `error_kind=bug`, `ConnectError: All connection attempts failed` | the plugin can't reach the API — check `PLUGIN_RUNNER_PLUGIN_API_URL` (or `PLUGIN_RUNNER_CATLICO_API_URL`) points at a running API |
| `error_kind=timeout` | plugin exceeded `timeout_seconds` (slow vendor) — raise the timeout or pick a faster target |
| `config_complete=false` | a required secret/setting is missing — add it via `secrets_env` / `--secret` |
