#!/usr/bin/env bash
# Start a plugin runner scoped to one or more plugins, in container isolation,
# wired so plugin containers can call back to a Catlico API on the host.
# Companion to e2e/e2e_check.py — see e2e/README.md.
#
#   ./e2e/start_runner.sh                          # default: observable-validator
#   ./e2e/start_runner.sh crtsh                    # scope to one plugin
#   ./e2e/start_runner.sh observable-validator abuseipdb crtsh   # several
#
# The runner builds ONE image per scoped plugin on startup, so scope to just the
# plugin(s) you're testing rather than all of catlico-plugins.
#
# Env overrides:
#   CATLICO_API_URL   runner -> API base       (default http://localhost:8000)
#   PLUGIN_API_URL    plugin container -> API  (default http://host.docker.internal:8000)
#   ENROLL_TOKEN      one-time enrollment token (only for first enroll / after a
#                     runner reset; resumes from .runner-state.json otherwise).
#                     NOTE: adding a NOT-yet-registered plugin needs a re-enroll,
#                     because the runner only reports its plugin set when it enrolls.
set -euo pipefail

RUNNER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$RUNNER_DIR/.." && pwd)"
SDK_DIR="$REPO_ROOT/catlico-plugin-sdk"
PLUGINS_DIR="$REPO_ROOT/catlico-plugins"

PLUGINS=("$@"); [ ${#PLUGINS[@]} -eq 0 ] && PLUGINS=("observable-validator")

# A plugin dir containing ONLY the requested plugins (symlinks), so the runner
# builds one image each and nothing else.
SCOPED_DIR="$RUNNER_DIR/e2e/.scoped-plugins"
rm -rf "$SCOPED_DIR" && mkdir -p "$SCOPED_DIR"
for p in "${PLUGINS[@]}"; do
  [ -f "$PLUGINS_DIR/$p/catlico-plugin.toml" ] || { echo "no such plugin: $p"; exit 1; }
  ln -s "$PLUGINS_DIR/$p" "$SCOPED_DIR/$p"
done

export PLUGIN_RUNNER_RUNNER_ID="${PLUGIN_RUNNER_RUNNER_ID:-runner-1}"
export PLUGIN_RUNNER_CATLICO_API_URL="${CATLICO_API_URL:-http://localhost:8000}"
# Plugin containers can't use the runner's localhost; on Docker Desktop the host
# is host.docker.internal. On native Linux, also set:
#   PLUGIN_RUNNER_CONTAINER_EXTRA_HOSTS='["host.docker.internal:host-gateway"]'
export PLUGIN_RUNNER_PLUGIN_API_URL="${PLUGIN_API_URL:-http://host.docker.internal:8000}"
export PLUGIN_RUNNER_ISOLATION_MODE=container
export PLUGIN_RUNNER_SDK_SOURCE="$SDK_DIR"
export PLUGIN_RUNNER_PLUGIN_DIRS="[\"$SCOPED_DIR\"]"
[ -n "${ENROLL_TOKEN:-}" ] && export PLUGIN_RUNNER_ENROLLMENT_TOKEN="$ENROLL_TOKEN"

echo "scoped plugins:           ${PLUGINS[*]}"
echo "runner -> API:            $PLUGIN_RUNNER_CATLICO_API_URL"
echo "plugin container -> API:  $PLUGIN_RUNNER_PLUGIN_API_URL"
echo "isolation:                container"
echo

cd "$RUNNER_DIR"
exec uv run plugin-runner
