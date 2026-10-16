# Local stand-ins for external SaaS. Compose and tests only; never deployed.
FROM ghcr.io/astral-sh/uv:0.9 AS uv
FROM python:3.12-slim
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PYTHON=/usr/local/bin/python3.12 \
    PYTHONUNBUFFERED=1 PATH=/app/.venv/bin:$PATH
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY fakes ./fakes
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev
RUN mkdir -p /data /sandboxes && chown 10001 /data /sandboxes
USER 10001
EXPOSE 4100
CMD ["uvicorn", "fakes.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "4100"]
