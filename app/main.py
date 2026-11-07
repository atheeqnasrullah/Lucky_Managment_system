"""FastAPI application entrypoint."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import invoices, tasks, webhook
from app.container import Container, build_container
from app.core.config import Settings, get_settings
from app.core.errors import BillingBotError, ExtractionError, NotFoundError
from app.core.logging import configure_logging, get_logger, trace_id_var
from app.core.security import hash_api_key
from app.models.domain import ApiKey, Tenant

log = get_logger(__name__)


DEV_TENANT_ID = "dev-tenant"


def seed_dev_api_key(container: Container) -> bool:
    """Local in-memory mode only: create a demo tenant + API key so /docs works out of the box."""
    s = container.settings
    if s.environment != "local" or s.storage_backend != "memory" or not s.dev_api_key:
        return False
    if not container.repo.get_tenant(DEV_TENANT_ID):
        container.repo.save_tenant(Tenant(id=DEV_TENANT_ID, name="Dev tenant"))
    container.repo.save_api_key(ApiKey(key_hash=hash_api_key(s.dev_api_key), tenant_id=DEV_TENANT_ID, name="dev"))
    return True


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.container = container or build_container(settings)
        if seed_dev_api_key(app.state.container):
            log.info(f"Local dev API key ready: use X-API-Key '{settings.dev_api_key}' (Authorize button in /docs)")
        log.info(
            "startup",
            extra={"extra_fields": {"env": settings.environment, "ai": settings.ai_backend, "storage": settings.storage_backend}},
        )
        yield

    app = FastAPI(
        title="WhatsApp AI Billing Bot",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
    )
    if container is not None:
        app.state.container = container

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        header = request.headers.get("X-Cloud-Trace-Context", "")
        trace_id = header.split("/")[0] or request.headers.get("X-Trace-Id") or uuid.uuid4().hex
        trace_id_var.set(trace_id)
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    @app.exception_handler(NotFoundError)
    async def not_found(_: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    @app.exception_handler(ExtractionError)
    async def upstream_error(_: Request, exc: ExtractionError) -> JSONResponse:
        # Transient AI-provider failure: 503 tells Cloud Tasks / API clients to retry.
        return JSONResponse(status_code=503, content={"detail": "AI service temporarily unavailable"})

    @app.exception_handler(BillingBotError)
    async def domain_error(_: Request, exc: BillingBotError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict:
        return {"status": "ok", "env": settings.environment}

    app.include_router(webhook.router)
    app.include_router(tasks.router)
    app.include_router(invoices.router)
    return app


app = create_app()
