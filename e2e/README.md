# Plugin runner — end-to-end check (reusable)

A repeatable, **plugin-agnostic** end-to-end test: feed an observable to a plugin,
let it run all the way through the runner's sandbox, and assert the `PluginResult`
that comes back — the exact row the web UI's **Plugin Results** panel displays.

```
create observable → observable.created → API HMAC-push to the runner
→ runner claims a run → plugin executes in a sandbox (container by default)
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
| **Hermetic sandbox** (`tests/test_sandbox_fake_api_e2e.py`) | `uv run pytest` | none (fake runtime API) | the plugin really executes in a sandbox and POSTs a well-formed result — **no API/DB/Docker** |
| Runner dispatch (`tests/test_engine.py`) | `uv run pytest` | none (mocked client) | claim/dispatch orchestration |
| **Full e2e** (`e2e/e2e_check.py`, this dir) | see below | API + DB + runner + Docker | the real dispatch → sandbox → persistence loop |

The hermetic sandbox test is the "run it for real without the API" answer: it points
a plugin's `ctx.api` at a ~40-line stdlib HTTP stub and runs it through the real
subprocess sandbox. Use the full e2e below only to confirm the real control plane.

## Files

| file | purpose |
|---|---|
| `e2e_check.py`   | the reusable checker (stdlib only). `python e2e/e2e_check.py <plugin-id>` |
| `scenarios.json` | per-plugin scenarios: observable to feed, config/secrets, what to assert |
| `start_runner.sh`| start a runner scoped to the plugin(s) under test, container isolation |
| `README.md`      | this file |

## Prerequisites (one-time per session)

Docker running, plus:

### 1. API — reachable *from inside a container*

`make dev` binds `127.0.0.1`, which a plugin container cannot reach. Bind `0.0.0.0`:

```bash
cd catlico-api
set -a; . ./.env; set +a
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 2. Runner — scoped to the plugin(s) under test, container isolation

```bash
cd catlico-plugin-runner
./e2e/start_runner.sh observable-validator            # one plugin
./e2e/start_runner.sh observable-validator ip-api     # several
```

The runner builds one image per scoped plugin, enrolls, and registers them with the
API. On the **first** enroll (or after a reset) pass a one-time token:

```bash
ENROLL_TOKEN=cpe_xxx ./e2e/start_runner.sh observable-validator
```

Mint a token in Catlico as superadmin: `POST /api/v1/plugin-runners` (new runner) or
`POST /api/v1/plugin-runners/{id}/re-enroll` (existing). Wait for
`<plugin>: installed` and `POST …/register 200` (first enroll) or `resumed runner …`
(later) in the runner log.

> **Adding a plugin the runner hasn't registered before requires a re-enroll** — the
> runner only reports its plugin set to the API when it enrolls, not on a plain
> resume. So to add `ip-api` to an already-running runner: re-enroll, then
> `start_runner.sh observable-validator ip-api` with the fresh `ENROLL_TOKEN`.

### 3. Web (optional, for eyeballing) — `cd catlico-web && pnpm dev` → http://localhost:3000

### Networking notes

- Plugin containers reach the host API via `host.docker.internal`
  (`PLUGIN_RUNNER_PLUGIN_API_URL`, default `http://host.docker.internal:8000`).
  Docker Desktop resolves that name automatically.
- On **native Linux**, also export
  `PLUGIN_RUNNER_CONTAINER_EXTRA_HOSTS='["host.docker.internal:host-gateway"]'`
  before `start_runner.sh` (Docker Desktop must NOT set this — there it breaks the
  built-in name).

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
| `error_kind=bug`, `ConnectError: All connection attempts failed` | plugin container can't reach the API — API not on `0.0.0.0`, or `PLUGIN_RUNNER_PLUGIN_API_URL` / `host.docker.internal` not resolving |
| `error_kind=timeout` | plugin exceeded `timeout_seconds` (slow vendor) — raise the timeout or pick a faster target |
| `config_complete=false` | a required secret/setting is missing — add it via `secrets_env` / `--secret` |
