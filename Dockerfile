# Build context is the repo root (see `make build`), because the runner depends
# on catlico-plugin-sdk as a sibling path dependency (../catlico-plugin-sdk) and
# uv must find it at that relative layout to resolve the lockfile.
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

WORKDIR /src/catlico-plugin-runner

# Carry over both trees so the venv (and its path dep) resolves at runtime.
COPY --from=builder /src /src

ENV PATH="/src/catlico-plugin-runner/.venv/bin:$PATH"

# Private API bind (PLUGIN_RUNNER_PORT, default 8090).
EXPOSE 8090

CMD ["plugin-runner"]
