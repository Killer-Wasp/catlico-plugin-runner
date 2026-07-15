# Build context is the repo root (see `make build`): the runner depends on
# catlico-plugin-sdk as a sibling path dependency (../catlico-plugin-sdk), and
# the default image bakes the full plugin catalog (catlico-plugins/) into /plugins.
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /src

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Bring in both trees, preserving the sibling layout the lock pins.
COPY catlico-plugin-sdk/ catlico-plugin-sdk/
COPY catlico-plugin-runner/ catlico-plugin-runner/

WORKDIR /src/catlico-plugin-runner

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM python:3.14-slim-bookworm

# The runtime stage needs uv on PATH: `plugin-runner sync` shells out to it to
# materialise each plugin's venv, both at build time (below) and on a cold start.
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /bin/uv

WORKDIR /src/catlico-plugin-runner

# Carry over both trees so the runner venv (and its path dep) resolves at runtime.
COPY --from=builder /src /src

ENV PATH="/src/catlico-plugin-runner/.venv/bin:$PATH"

# Bake the full plugin catalog and pre-sync every plugin's venv in one layer, so a
# container starts with zero syncs (warm-start markers already present). Venvs and
# the uv cache live under /var/cache/catlico (mount a persistent volume there to
# share/keep them). The uv cache is dropped from this layer to keep the image lean.
#   Custom images: FROM catlico/plugin-runner, then
#   `RUN plugin-runner install <git-url> --ref <ref> && plugin-runner sync`.
COPY catlico-plugins/ /plugins/
ENV PLUGIN_RUNNER_PLUGINS_DIR=/plugins
RUN UV_LINK_MODE=copy plugin-runner sync \
 && rm -rf /var/cache/catlico/uv

# Private API bind (PLUGIN_RUNNER_PORT, default 8090).
EXPOSE 8090

CMD ["plugin-runner", "serve"]
