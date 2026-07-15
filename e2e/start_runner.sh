#!/usr/bin/env bash
# Start a plugin runner scoped to one or more plugins, provisioning a per-plugin
# venv for each and running them as plain subprocesses (no container).
# Companion to e2e/e2e_check.py — see e2e/README.md.
#
#   ./e2e/start_runner.sh                          # default: observable-validator
#   ./e2e/start_runner.sh crtsh                    # scope to one plugin
#   ./e2e/start_runner.sh observable-validator abuseipdb crtsh   # several
#
# The runner syncs ONE venv per scoped plugin on startup (marker-skipped when
# warm), so scope to just the plugin(s) you're testing.
#
# Env overrides:
#   CATLICO_API_URL   runner -> API base     (default http://localhost:8000)
#   PLUGIN_API_URL    plugin -> API base      (default: reuses CATLICO_API_URL)
set -euo pipefail

RUNNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$RUNNER_DIR/.." && pwd)"
SDK_DIR="$REPO_ROOT/catlico-plugin-sdk"
PLUGINS_DIR="$REPO_ROOT/catlico-plugins"

PLUGINS=("$@"); [ ${#PLUGINS[@]} -eq 0 ] && PLUGINS=("observable-validator")

# A plugins dir containing ONLY the requested plugins (symlinks), so the runner
# provisions one venv each and nothing else.
SCOPED_DIR="$RUNNER_DIR/e2e/.scoped-plugins"
rm -rf "$SCOPED_DIR" && mkdir -p "$SCOPED_DIR"
for p in "${PLUGINS[@]}"; do
  [ -f "$PLUGINS_DIR/$p/catlico-plugin.toml" ] || { echo "no such plugin: $p"; exit 1; }
  ln -s "$PLUGINS_DIR/$p" "$SCOPED_DIR/$p"
done

export PLUGIN_RUNNER_RUNNER_ID="${PLUGIN_RUNNER_RUNNER_ID:-runner-1}"
export PLUGIN_RUNNER_CATLICO_API_URL="${CATLICO_API_URL:-http://localhost:8000}"
export PLUGIN_RUNNER_PLUGIN_API_URL="${PLUGIN_API_URL:-}"
export PLUGIN_RUNNER_PLUGINS_DIR="$SCOPED_DIR"
# Dev: install the SDK editable into each plugin venv from the sibling checkout,
# so plugins run against the SDK-under-development.
export PLUGIN_RUNNER_SDK_SOURCE="$SDK_DIR"

echo "scoped plugins:  ${PLUGINS[*]}"
echo "runner -> API:   $PLUGIN_RUNNER_CATLICO_API_URL"
echo "plugins dir:     $PLUGIN_RUNNER_PLUGINS_DIR"
echo "execution:       subprocess (per-plugin venv, no sandbox)"
echo

cd "$RUNNER_DIR"
exec uv run plugin-runner serve
