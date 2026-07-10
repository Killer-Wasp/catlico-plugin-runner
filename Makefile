.PHONY: install run dev test build image

# Sync dependencies (including the dev group).
install:
	uv sync

# Run the runner. Enrolls with the control plane, starts the heartbeat loop, and
# serves the private API on PLUGIN_RUNNER_PORT (default 8090). Requires the
# Catlico API reachable at PLUGIN_RUNNER_CATLICO_API_URL.
run:
	set -a; \
	[ ! -f .env ] || . ./.env; \
	: "$${PLUGIN_RUNNER_CATLICO_API_URL:=http://localhost:8000}"; \
	set +a; \
	uv run plugin-runner

# Like `run`, but auto-restarts when runner/sdk code changes. Handy while
# developing a plugin. watchfiles is fetched on demand.
dev:
	set -a; \
	[ ! -f .env ] || . ./.env; \
	: "$${PLUGIN_RUNNER_CATLICO_API_URL:=http://localhost:8000}"; \
	set +a; \
	uv run --with watchfiles -- watchfiles --target-type command "plugin-runner" plugin_runner ../catlico-plugin-sdk

# Run the test suite.
test:
	uv run pytest

# Build the runner container image. Context is the repo root because the image
# needs the sibling catlico-plugin-sdk path dependency.
build image:
	docker build -t catlico-plugin-runner -f Dockerfile ..
