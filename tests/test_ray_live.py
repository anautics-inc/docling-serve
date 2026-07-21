"""Real Ray-cluster acceptance for the owned canonical coordinator."""

from __future__ import annotations

import os

import pytest
import ray


@ray.remote
def _canonical_round_trip() -> dict:
    import asyncio
    from io import BytesIO
    from pathlib import Path
    from types import SimpleNamespace

    from docling.datamodel.base_models import DocumentStream
    from docling.datamodel.service.responses import ChunkedDocumentResult
    from docling_jobkit.datamodel.result import DoclingTaskResult
    from docling_jobkit.datamodel.task import Task, TaskType

    from docling_serve.ingestion.canonical_result import canonical_from_task_result
    from docling_serve.ray_legacy import (
        CanonicalRayCoordinatorDeployment,
    )

    task = Task(
        task_id="ray-live-canonical",
        task_type=TaskType.CHUNK,
        sources=[
            DocumentStream(
                name="ray-live.md",
                stream=BytesIO(b"# Ray canonical acceptance"),
            )
        ],
        metadata={
            "tenant_id": "ray-acceptance",
            "canonical_ingestion": {
                "profile": "document",
                "ocr_policy": "auto",
            },
        },
    )
    raw = DoclingTaskResult(
        num_converted=1,
        num_succeeded=1,
        num_failed=0,
        processing_time=0.01,
        result=ChunkedDocumentResult(chunks=[], documents=[]),
    )
    coordinator_type = CanonicalRayCoordinatorDeployment.func_or_class
    base_type = coordinator_type.__mro__[1]
    parent_process = base_type._process_task

    async def converted_result(_self, _task, _workdir):
        return raw

    base_type._process_task = converted_result
    try:
        coordinator = object.__new__(coordinator_type)
        coordinator.config = SimpleNamespace(results_ttl=30)
        result = asyncio.run(
            coordinator_type._process_task(coordinator, task, Path("/tmp"))
        )
    finally:
        base_type._process_task = parent_process
    canonical = canonical_from_task_result(result)
    assert canonical is not None
    return canonical.model_dump(mode="json", by_alias=True)


@pytest.mark.integration
def test_real_ray_cluster_serializes_canonical_result() -> None:
    address = os.getenv("DOCLING_SERVE_TEST_RAY_ADDRESS", "").strip()
    if not address:
        pytest.skip("DOCLING_SERVE_TEST_RAY_ADDRESS is not configured")
    ray.init(address=address, ignore_reinit_error=True)
    try:
        result = ray.get(_canonical_round_trip.remote(), timeout=60)
    finally:
        ray.shutdown()
    assert result["contract"] == "docling.canonical-ingestion.v1"
    assert result["routing"]["domain"] == "document"
