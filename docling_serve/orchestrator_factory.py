import importlib
import logging
from functools import lru_cache
from typing import Any

from docling_jobkit.orchestrators.base_orchestrator import BaseOrchestrator

from docling_serve.jobkit_config import (
    AsyncEngine,
    build_converter_manager_config,
    build_local_orchestrator_config,
    build_ray_orchestrator_config,
    build_rq_orchestrator_config,
    current_async_engine,
    traces_enabled,
)
from docling_serve.legacy_office import build_converter_manager

_log = logging.getLogger(__name__)

# Compatibility facades retained for existing operational scripts and imports.
_build_cm_config = build_converter_manager_config
__all__ = [
    "_build_cm_config",
    "build_converter_manager",
    "get_async_orchestrator",
]


def __getattr__(name: str) -> Any:
    if name == "docling_serve_settings":
        return importlib.import_module("docling_serve.settings").docling_serve_settings
    raise AttributeError(name)


@lru_cache
def get_async_orchestrator() -> BaseOrchestrator:
    engine = current_async_engine()
    if engine == AsyncEngine.LOCAL:
        from docling_serve.local_orchestrator import DoclingServeLocalOrchestrator

        return DoclingServeLocalOrchestrator(
            config=build_local_orchestrator_config(),
            converter_manager=build_converter_manager(
                config=build_converter_manager_config()
            ),
        )

    if engine == AsyncEngine.RQ:
        from docling_serve.rq_instrumentation import wrap_rq_queue_for_tracing
        from docling_serve.rq_orchestrator import DoclingServeRQOrchestrator

        orchestrator = DoclingServeRQOrchestrator(config=build_rq_orchestrator_config())
        # This path is a deployment contract consumed by existing RQ workers.
        orchestrator._rq_job_function = (
            "docling_serve.rq_job_wrapper.instrumented_docling_task"
        )
        if traces_enabled():
            wrap_rq_queue_for_tracing(orchestrator._rq_queue)
        return orchestrator

    if engine == AsyncEngine.RAY:
        from docling_serve.ray_legacy import DoclingServeRayOrchestrator

        return DoclingServeRayOrchestrator(
            config=build_ray_orchestrator_config(),
            converter_manager=build_converter_manager(
                config=build_converter_manager_config()
            ),
        )

    raise RuntimeError(f"Engine {engine} not recognized.")
