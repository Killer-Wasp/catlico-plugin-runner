#!/usr/bin/env python3
"""Reusable end-to-end check: drive ANY plugin through the runner and assert it.

Proves the full loop for one plugin: create an entity (observable), let Catlico
dispatch ``<trigger>`` to the runner, have the runner execute the plugin in a
subprocess bound to its per-plugin venv, and assert the ``PluginResult`` that comes
back — the same row the web UI's **Plugin Results** panel displays.

    create observable → observable.created → API HMAC-push → runner claims a run
    → plugin runs in a subprocess (its own venv python) → ctx.api writes a result
    → this script asserts it.

It is **plugin-agnostic**: a scenario names the plugin, the observable to feed it,
any config/secrets it needs, and what to assert. Scenarios live in
``e2e/scenarios.json``; select one by plugin id, or pass everything on the CLI.

    # a named scenario from scenarios.json
    python e2e/e2e_check.py observable-validator
    python e2e/e2e_check.py crtsh

    # an ad-hoc scenario (no file entry needed)
    python e2e/e2e_check.py abuseipdb --type ip --value 118.25.6.39 \
        --secret key=$ABUSEIPDB_API_KEY --expect-source AbuseIPDB

The plugin must be **registered + available on a healthy runner** first — scope a
runner to it with ``e2e/start_runner.sh <plugin-id>``. Stdlib only; exit 0 = PASS.

Prerequisites and the full walkthrough are in ``e2e/README.md``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = os.environ.get("CATLICO_API_URL", "http://localhost:8000").rstrip("/")
ADMIN_EMAIL = os.environ.get("CATLICO_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("CATLICO_ADMIN_PASSWORD", "changeme")
ORG = os.environ.get("CATLICO_ORG", "catlico-demo")
TIMEOUT_S = int(os.environ.get("CATLICO_E2E_TIMEOUT", "60"))
SCENARIOS_FILE = Path(__file__).resolve().parent / "scenarios.json"


def _req(method: str, path: str, token: str | None = None, body: dict | None = None) -> tuple[int, object]:
    url = f"{API}/api/v1{path}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json", "X-Organisation-Id": ORG}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, raw.decode(errors="replace")
    except urllib.error.URLError as exc:
        return 0, str(exc)


def fail(msg: str) -> None:
    print(f"\n✗ FAIL: {msg}")
    sys.exit(1)


def load_scenario(args: argparse.Namespace) -> dict:
    """Merge a scenarios.json entry (if any) with CLI overrides."""
    scenario: dict = {}
    if SCENARIOS_FILE.exists():
        scenario = json.loads(SCENARIOS_FILE.read_text()).get(args.plugin_id, {})
    scenario = dict(scenario)  # copy

    if args.type:
        scenario["observable_type"] = args.type
    if args.value:
        scenario["observable_value"] = args.value
    if args.setting:
        scenario.setdefault("settings", {}).update(dict(kv.split("=", 1) for kv in args.setting))
    # --secret key=VALUE takes a literal value; scenarios.json uses secrets_env
    # (param -> env var name) so secrets never live in the file.
    secrets = dict(scenario.get("secrets", {}))
    for param, env_var in (scenario.get("secrets_env") or {}).items():
        val = os.environ.get(env_var)
        if not val:
            fail(f"scenario needs secret {param!r} from env {env_var} — export it first")
        secrets[param] = val
    for kv in args.secret or []:
        k, v = kv.split("=", 1)
        secrets[k] = v
    scenario["secrets"] = secrets

    expect = dict(scenario.get("expect", {}))
    if args.expect_verdict:
        expect["verdict"] = args.expect_verdict
    if args.expect_source:
        expect["source"] = args.expect_source
    scenario["expect"] = expect

    if not scenario.get("observable_type") or not scenario.get("observable_value"):
        fail(f"no scenario for {args.plugin_id!r} in scenarios.json and no --type/--value given")
    return scenario


def main() -> None:
    ap = argparse.ArgumentParser(description="Reusable plugin e2e check")
    ap.add_argument("plugin_id", help="plugin id to test (e.g. observable-validator)")
    ap.add_argument("--type", help="observable_type (ip/domain/fqdn/url/mail/hash)")
    ap.add_argument("--value", help="observable value to feed the plugin")
    ap.add_argument("--setting", action="append", metavar="k=v", help="non-secret config (repeatable)")
    ap.add_argument("--secret", action="append", metavar="k=v", help="secret config, literal value (repeatable)")
    ap.add_argument("--expect-verdict", help="assert the result verdict equals this")
    ap.add_argument("--expect-source", help="assert the result source equals this")
    args = ap.parse_args()

    plugin_id = args.plugin_id
    sc = load_scenario(args)
    otype, value, expect = sc["observable_type"], sc["observable_value"], sc["expect"]
    print(f"e2e: API={API} org={ORG} plugin={plugin_id} observable={otype}:{value}")

    # 1. Authenticate.
    status, body = _req("POST", "/auth/login", body={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    if status != 200 or not isinstance(body, dict) or "access_token" not in body:
        fail(f"login failed ({status}): {body}")
    token = body["access_token"]
    print("  ✓ authenticated")

    # 2. Plugin must be registered by a live runner and available.
    status, pdef = _req("GET", f"/plugins/{plugin_id}", token)
    if status != 200:
        fail(f"plugin {plugin_id!r} not registered ({status}). Start it: e2e/start_runner.sh {plugin_id}")
    if not pdef.get("available"):
        fail(f"plugin {plugin_id!r} has no healthy runner (available=false). e2e/start_runner.sh {plugin_id}")
    print(f"  ✓ plugin registered + available on runner(s) {pdef.get('runner_ids')}")

    # 3. Config (settings + secrets) if the scenario supplies any.
    if sc.get("settings") or sc.get("secrets"):
        status, _ = _req("PUT", f"/plugins/{plugin_id}/config", token,
                         body={"settings": sc.get("settings", {}), "secrets": sc.get("secrets", {})})
        if status not in (200, 201, 204):
            fail(f"config update failed ({status})")
        print(f"  ✓ config set (settings={list(sc.get('settings', {}))} secrets={list(sc.get('secrets', {}))})")

    # 4. Enable + auto-run (idempotent).
    _req("POST", f"/plugins/{plugin_id}/enable", token, body={})
    _req("POST", f"/plugins/{plugin_id}/auto-run/enable", token, body={})
    status, pdef = _req("GET", f"/plugins/{plugin_id}", token)
    if not (pdef.get("enabled") and pdef.get("auto_run_enabled")):
        fail(f"could not enable + auto-run plugin: {pdef}")
    if not pdef.get("config_complete", True):
        fail(f"plugin {plugin_id!r} config is incomplete — supply required settings/secrets in the scenario")
    print("  ✓ plugin enabled + auto-run for org")

    # 5. Fresh case + observable — fires observable.created.
    status, case = _req("POST", "/cases/", token, body={"title": f"E2E — {plugin_id}", "severity": 2})
    if status not in (200, 201):
        fail(f"case create failed ({status}): {case}")
    case_id = case["id"]
    status, obs = _req("POST", f"/cases/{case_id}/observables", token,
                      body={"observable_type": otype, "data": value, "message": f"e2e {plugin_id}"})
    if status not in (200, 201):
        fail(f"observable create failed ({status}): {obs}")
    obs_id = obs["id"]
    print(f"  ✓ created case #{case_id} + observable {otype}:{value} ({obs_id})")

    # 6. Poll for the PluginResult from THIS plugin.
    print(f"  … waiting up to {TIMEOUT_S}s for the plugin result", end="", flush=True)
    deadline = time.monotonic() + TIMEOUT_S
    mine: list = []
    while time.monotonic() < deadline:
        status, body = _req("GET", f"/observables/{obs_id}/plugin-results", token)
        rows = body if isinstance(body, list) else (body.get("items", []) if isinstance(body, dict) else [])
        mine = [r for r in rows if r.get("plugin_id") == plugin_id]
        if mine:
            break
        print(".", end="", flush=True)
        time.sleep(2)
    print()
    if not mine:
        fail(f"no result from {plugin_id!r} after {TIMEOUT_S}s — check the runner log + plugin_run rows")

    r = mine[0]
    print(f"  ✓ got PluginResult: verdict={r.get('verdict')!r} source={r.get('source')!r} "
          f"summary={r.get('summary')!r}")

    # 7. Assert per the scenario's expectations.
    problems = []
    if "verdict" in expect and r.get("verdict") != expect["verdict"]:
        problems.append(f"verdict {r.get('verdict')!r} != {expect['verdict']!r}")
    if "source" in expect and r.get("source") != expect["source"]:
        problems.append(f"source {r.get('source')!r} != {expect['source']!r}")
    nd = r.get("normalized_data") or {}
    for k, want in (expect.get("normalized") or {}).items():
        if nd.get(k) != want:
            problems.append(f"normalized[{k!r}] {nd.get(k)!r} != {want!r}")
    if problems:
        fail("; ".join(problems))

    print(f"\n✓ PASS — {plugin_id} produced a result for {otype}:{value} end-to-end through the "
          f"runner (case #{case_id}). The web UI 'Plugin Results' panel reads this same row.")


if __name__ == "__main__":
    main()
