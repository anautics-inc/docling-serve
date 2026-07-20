from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from docling_serve import jobkit_config
from docling_serve.__main__ import _build_rq_worker_configs
from docling_serve.execution import staged_docling_task


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["local", "rq", "ray"])
@pytest.mark.parametrize("fails", [False, True])
async def test_staged_task_lifecycle_is_identical_across_engines(
    monkeypatch: pytest.MonkeyPatch,
    engine: str,
    fails: bool,
) -> None:
    events: list[str] = []
    task = SimpleNamespace(task_id="task-1")
    worker_task = SimpleNamespace(task_id="task-1-materialized")

    @contextmanager
    def materialize(_task: Any):
        events.append("materialize")
        yield worker_task

    def cleanup_sync(_task: Any) -> None:
        events.append("cleanup")

    async def cleanup_async(_task: Any) -> None:
        events.append("cleanup")

    monkeypatch.setattr(staged_docling_task, "materialize_staged_task", materialize)
    monkeypatch.setattr(
        staged_docling_task,
        "cleanup_task_staged_uploads_sync",
        cleanup_sync,
    )
    monkeypatch.setattr(
        staged_docling_task,
        "cleanup_task_staged_uploads",
        cleanup_async,
    )
    monkeypatch.setattr(
        staged_docling_task,
        "staged_refs_for_task",
        lambda _task: [],
    )

    def failure_handler(_exc: Exception, _failure: Any) -> None:
        events.append("map")

    def run_sync(received: Any) -> str:
        assert received is worker_task
        events.append("run")
        if fails:
            raise RuntimeError("conversion failed")
        return "result"

    async def run_async(received: Any) -> str:
        return run_sync(received)

    if fails:
        with pytest.raises(RuntimeError, match="conversion failed"):
            if engine == "ray":
                await staged_docling_task.run_staged_task_async(
                    task,
                    run_async,
                    on_failure=failure_handler,
                )
            else:
                staged_docling_task.run_staged_task(
                    task,
                    run_sync,
                    on_failure=failure_handler,
                )
        assert events == ["materialize", "run", "map", "cleanup"]
    else:
        result = (
            await staged_docling_task.run_staged_task_async(task, run_async)
            if engine == "ray"
            else staged_docling_task.run_staged_task(task, run_sync)
        )
        assert result == "result"
        assert events == ["materialize", "run", "cleanup"]


def test_rq_cli_and_service_configs_have_parity() -> None:
    cli_rq_config, cli_converter_config = _build_rq_worker_configs()

    assert cli_rq_config == jobkit_config.build_rq_orchestrator_config()
    assert cli_converter_config == jobkit_config.build_converter_manager_config()


def test_stable_worker_and_ray_import_paths() -> None:
    from docling_serve.ray_legacy import (
        DoclingServeRayOrchestrator,
        LegacyOfficeRayConverterDeployment,
        create_legacy_deployment,
        deploy_legacy_processor,
    )
    from docling_serve.rq_job_wrapper import instrumented_docling_task

    assert (
        f"{instrumented_docling_task.__module__}.{instrumented_docling_task.__name__}"
        == "docling_serve.rq_job_wrapper.instrumented_docling_task"
    )
    assert DoclingServeRayOrchestrator.__module__ == "docling_serve.ray_legacy"
    assert LegacyOfficeRayConverterDeployment is not None
    assert create_legacy_deployment.__module__ == "docling_serve.ray_legacy"
    assert deploy_legacy_processor.__module__ == "docling_serve.ray_legacy"
