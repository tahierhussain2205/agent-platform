# syntax=docker/dockerfile:1
# One image for every backend process: api, worker, maintenance, migrate, seed.

FROM ghcr.io/astral-sh/uv:0.9 AS uv

FROM python:3.12-slim AS builder
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3.12
WORKDIR /app
# Dependencies first (cached unless the lock changes), then the project itself.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY migrations ./migrations
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

FROM python:3.12-slim AS runtime
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /app app
WORKDIR /app
COPY --from=builder --chown=root:root /app /app
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000
USER 10001:10001
EXPOSE 8000
# No ENTRYPOINT on purpose: Render's dockerCommand / preDeployCommand and Compose's `command`
# replace CMD with a full command (`app worker`, `app migrate`, ...). Exec form, so the process
# is PID 1 and receives SIGTERM directly; each command handles it.
CMD ["app", "api"]
