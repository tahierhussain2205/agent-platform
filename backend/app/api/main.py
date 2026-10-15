"""FastAPI application factory."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="Agent Platform API", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        """Process is up. Readiness (Postgres, Key Value) is a separate check."""
        return {"status": "ok"}

    return app
