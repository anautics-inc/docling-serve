from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from docling.datamodel.base_models import DocumentStream
from docling.datamodel.service.responses import ChunkedDocumentResult
from docling.datamodel.service.tasks import TaskType
from docling_jobkit.datamodel.result import DoclingTaskResult
from docling_jobkit.datamodel.task import Task

from docling_serve.ingestion.canonical_result import (
    CANONICAL_INFO_KEY,
    CanonicalRouting,
    CanonicalTaskResult,
    attach_canonical_result,
    canonical_from_task_result,
)
from docling_serve.ingestion.canonical_task import (
    finalize_canonical_task,
    prepare_canonical_task,
)


def _chunk_result() -> DoclingTaskResult:
    return DoclingTaskResult(
        num_converted=1,
        num_succeeded=1,
        num_failed=0,
        processing_time=0.25,
        result=ChunkedDocumentResult(chunks=[], documents=[]),
    )


def test_canonical_envelope_round_trips_through_jobkit_chunking_info() -> None:
    result = _chunk_result()
    canonical = CanonicalTaskResult(
        markdown="# Document",
        chunks=[],
        routing=CanonicalRouting(
            domain="document",
            reason="generic supported format",
            ocrPolicy="auto",
        ),
        processingTime=0.25,
    )

    attached = attach_canonical_result(result, canonical)

    assert attached.result.chunking_info is not None
    assert CANONICAL_INFO_KEY in attached.result.chunking_info
    assert canonical_from_task_result(attached) == canonical


def test_access_source_is_prepared_inside_canonical_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "docling_serve.access.extract.extract_access",
        lambda _path: (
            "# Inventory\n\n| id |\n| --- |\n| 1 |\n",
            [{"name": "Items"}],
            [{"name": "Items", "columns": ["id"], "rows": [{"id": "1"}]}],
        ),
    )
    monkeypatch.setattr(
        "docling_serve.access.extract.dump_schema", lambda _path: "Items (id)"
    )
    task = Task(
        task_id="task-access",
        task_type=TaskType.CHUNK,
        sources=[DocumentStream(name="inventory.accdb", stream=BytesIO(b"access"))],
        metadata={
            "tenant_id": "tenant-a",
            "canonical_ingestion": {"profile": "auto", "ocr_policy": "auto"},
        },
    )

    with prepare_canonical_task(task) as prepared:
        assert prepared is not None
        assert prepared.original_name == "inventory.accdb"
        assert prepared.decision.domain == "access"
        source = prepared.task.sources[0]
        assert isinstance(source, DocumentStream)
        assert source.name == "inventory.accdb.md"
        assert source.stream.read().startswith(b"# Inventory")
        assert prepared.source_metadata == {
            "filename": "inventory.accdb",
            "tables": [{"name": "Items"}],
            "schema": "Items (id)",
            "tabular": {
                "format": "captify.access/v1",
                "tables": [
                    {"name": "Items", "columns": ["id"], "rows": [{"id": "1"}]}
                ],
            },
        }


def test_xfa_form_markdown_is_prepared_inside_canonical_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "docling_serve.form.extract_xfa_form",
        lambda _path, *, source_key: {
            "source": source_key,
            "markdown": "# Access Request\n\n| Field | Value |\n| --- | --- |\n| Name | Ada |",
        },
    )
    task = Task(
        task_id="task-form",
        task_type=TaskType.CHUNK,
        sources=[DocumentStream(name="request.pdf", stream=BytesIO(b"%PDF-1.7"))],
        metadata={
            "tenant_id": "tenant-a",
            "canonical_ingestion": {"profile": "form", "ocr_policy": "auto"},
        },
    )

    with prepare_canonical_task(task) as prepared:
        assert prepared is not None
        assert prepared.decision.domain == "form"
        source = prepared.task.sources[0]
        assert isinstance(source, DocumentStream)
        assert source.name == "request.pdf.md"
        assert source.stream.read().startswith(b"# Access Request")


def test_auto_routing_uses_converted_markdown_before_typed_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = Task(
        task_id="task-auto",
        task_type=TaskType.CHUNK,
        sources=[DocumentStream(name="manual.pdf", stream=BytesIO(b"%PDF-1.7"))],
        metadata={
            "tenant_id": "tenant-a",
            "canonical_ingestion": {"profile": "auto", "ocr_policy": "auto"},
        },
    )
    seen: list[str] = []
    monkeypatch.setattr(
        "docling_serve.ingestion.canonical_task._markdown_from_result",
        lambda _result: "FIGURE & INDEX\nPART NUMBER\nSMR CODE",
    )

    def typed(context, **_kwargs):
        seen.append(context.decision.domain)
        return None

    monkeypatch.setattr("docling_serve.ingestion.canonical_task._typed_metadata", typed)
    with prepare_canonical_task(task) as prepared:
        assert prepared is not None
        assert prepared.decision.domain == "document"
        result = finalize_canonical_task(prepared, _chunk_result())

    canonical = canonical_from_task_result(result)
    assert canonical is not None
    assert canonical.routing.domain == "technical-order"
    assert seen == ["technical-order"]


@pytest.mark.asyncio
async def test_ray_coordinator_decorates_the_same_canonical_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from docling_serve import ray_legacy

    async def process(_self, _task, _workdir):
        return _chunk_result()

    monkeypatch.setattr(ray_legacy._BaseCoordinatorReplica, "_process_task", process)
    coordinator_type = ray_legacy.CanonicalRayCoordinatorDeployment.func_or_class
    coordinator = object.__new__(coordinator_type)
    coordinator.config = SimpleNamespace(results_ttl=60)
    coordinator._staging_redis = object()
    task = Task(
        task_id="task-ray",
        task_type=TaskType.CHUNK,
        sources=[DocumentStream(name="notes.md", stream=BytesIO(b"# Notes"))],
        metadata={
            "tenant_id": "tenant-a",
            "canonical_ingestion": {"profile": "auto", "ocr_policy": "auto"},
        },
    )

    result = await coordinator._process_task(task, tmp_path)

    canonical = canonical_from_task_result(result)
    assert canonical is not None
    assert canonical.routing.domain == "document"
