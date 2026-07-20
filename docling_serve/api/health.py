"""Health, readiness, and capability routes."""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import PlainTextResponse

from docling.datamodel.service.responses import HealthCheckResponse, ReadinessResponse

from docling_serve.api.deps import ApiDependencies
from docling_serve.ingestion.adapters import adapter_readiness, public_capabilities
from docling_serve.otel_instrumentation import get_metrics_endpoint_content

_log = logging.getLogger(__name__)


def create_health_router(deps: ApiDependencies) -> APIRouter:
    router = APIRouter()

    @router.get("/health", tags=["health"])
    def health() -> HealthCheckResponse:
        _log.info("Health check requested")
        return HealthCheckResponse()

    @router.get("/ready", tags=["health"])
    async def readiness() -> ReadinessResponse:
        await deps.assert_ready()
        return ReadinessResponse()

    @router.get("/readyz", tags=["health"], include_in_schema=False)
    async def readyz() -> ReadinessResponse:
        return await readiness()

    @router.get("/ready/adapters", tags=["health"])
    async def adapters() -> dict[str, dict[str, bool]]:
        await deps.assert_ready()
        return {"adapters": adapter_readiness()}

    @router.get("/v1/capabilities", tags=["health"])
    async def capabilities() -> dict[str, list[dict[str, object]]]:
        return {"capabilities": public_capabilities()}

    @router.get("/livez", tags=["health"], include_in_schema=False)
    async def livez() -> HealthCheckResponse:
        if deps.queue_processor_failed.is_set():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Background queue processor is not running.",
            )
        return HealthCheckResponse()

    @router.get("/api", include_in_schema=False)
    def api_check() -> HealthCheckResponse:
        return HealthCheckResponse()

    @router.get("/metrics", tags=["health"], include_in_schema=False)
    def metrics() -> PlainTextResponse:
        return PlainTextResponse(
            content=get_metrics_endpoint_content(),
            media_type="text/plain; version=0.0.4",
        )

    return router
