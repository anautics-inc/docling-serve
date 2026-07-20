"""Single composition boundary between the FastAPI shell and domain routers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from docling_serve.api.convert_chunk import create_convert_chunk_router
from docling_serve.api.deps import ApiDependencies
from docling_serve.api.extraction import create_extraction_router
from docling_serve.api.health import create_health_router
from docling_serve.api.management import create_management_router
from docling_serve.api.schematic import create_schematic_router
from docling_serve.api.tasks import create_tasks_router
from docling_serve.helper_functions import DOCLING_VERSIONS
from docling_serve.public_errors import build_public_http_detail
from docling_serve.storage import get_scratch


def compose_api(
    app: FastAPI,
    deps: ApiDependencies,
    *,
    settings: Any,
    offline_docs_assets: bool,
    downgrade_openapi: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    app.include_router(
        create_management_router(
            settings=settings,
            versions=DOCLING_VERSIONS,
            offline_docs_assets=offline_docs_assets,
            downgrade_openapi=downgrade_openapi,
        )
    )
    for factory in (
        create_health_router,
        create_convert_chunk_router,
        create_extraction_router,
        create_schematic_router,
        create_tasks_router,
    ):
        app.include_router(factory(deps))


__all__ = [
    "ApiDependencies",
    "build_public_http_detail",
    "compose_api",
    "get_scratch",
]
