"""Documentation and service-management routes."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse


def create_management_router(
    *,
    settings: Any,
    versions: dict[str, Any],
    offline_docs_assets: bool,
    downgrade_openapi: Callable[[dict[str, Any]], dict[str, Any]],
) -> APIRouter:
    router = APIRouter()

    @router.get("/openapi-3.0.json")
    def openapi_30(request: Request) -> JSONResponse:
        downgraded = downgrade_openapi(request.app.openapi())
        downgraded["openapi"] = "3.0.3"
        return JSONResponse(downgraded)

    @router.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> RedirectResponse:
        logo_url = (
            "/static/logo.svg"
            if offline_docs_assets
            else "https://raw.githubusercontent.com/docling-project/docling/"
            "refs/heads/main/docs/assets/logo.svg"
        )
        return RedirectResponse(url=logo_url)

    @router.get("/version", tags=["health"])
    def version_info() -> dict[str, Any]:
        if not settings.show_version_info:
            raise HTTPException(
                status_code=403,
                detail="Forbidden. The server is configured for not showing version details.",
            )
        return versions

    return router
