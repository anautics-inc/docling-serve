from io import BytesIO
from pathlib import Path

import pytest

from docling.datamodel.base_models import DocumentStream
from docling.datamodel.service.options import ConvertDocumentsOptions
from docling.datamodel.service.targets import InBodyTarget
from docling_jobkit.orchestrators.local.orchestrator import LocalOrchestratorConfig

from docling_serve.local_orchestrator import DoclingServeLocalOrchestrator


@pytest.mark.asyncio
async def test_real_local_orchestrator_retains_tenant_through_result_lookup(
    tmp_path: Path,
):
    manager = type("Manager", (), {"config": object()})()
    orchestrator = DoclingServeLocalOrchestrator(
        config=LocalOrchestratorConfig(
            num_workers=1,
            shared_models=True,
            scratch_dir=tmp_path,
        ),
        converter_manager=manager,
    )
    task = await orchestrator.enqueue(
        sources=[DocumentStream(name="report.pdf", stream=BytesIO(b"pdf"))],
        target=InBodyTarget(),
        convert_options=ConvertDocumentsOptions(),
        metadata={"tenant_id": "tenant-local"},
    )
    assert (await orchestrator.task_status(task.task_id)).metadata == {
        "tenant_id": "tenant-local"
    }

    sentinel_result = object()
    orchestrator._task_results[task.task_id] = sentinel_result
    assert await orchestrator.task_result(task.task_id) is sentinel_result
    assert (await orchestrator.get_raw_task(task.task_id)).metadata["tenant_id"] == (
        "tenant-local"
    )
