"""Credential-free Redis/RQ acceptance; enabled on the distributed test tier."""

from __future__ import annotations

import base64
import os
from pathlib import Path

import msgpack
import pytest
from redis import Redis
from rq import Queue, SimpleWorker

from docling.datamodel.service.responses import ChunkedDocumentResult
from docling.datamodel.service.sources import FileSource
from docling.datamodel.service.tasks import TaskType
from docling_jobkit.datamodel.result import DoclingTaskResult
from docling_jobkit.datamodel.task import Task
from docling_jobkit.orchestrators.rq.orchestrator import RQOrchestratorConfig
from docling_jobkit.orchestrators.serialization import make_msgpack_safe

from docling_serve.ingestion.canonical_result import canonical_from_task_result
from docling_serve.rq_job_wrapper import instrumented_docling_task


def _rq_echo(value: str) -> str:
    return value


@pytest.mark.integration
def test_real_redis_rq_enqueue_execute_result_and_cleanup() -> None:
    redis_url = os.getenv("DOCLING_SERVE_TEST_REDIS_URL", "").strip()
    if not redis_url:
        pytest.skip("DOCLING_SERVE_TEST_REDIS_URL is not configured")
    connection = Redis.from_url(redis_url)
    queue = Queue("docling-path-validation", connection=connection)
    job = queue.enqueue(_rq_echo, "canonical", result_ttl=30, failure_ttl=30)

    SimpleWorker([queue], connection=connection).work(burst=True)
    job.refresh()

    assert job.result == "canonical"
    job.delete()
    assert connection.exists(job.key) == 0


@pytest.mark.integration
def test_real_redis_rq_executes_and_stores_canonical_task(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    redis_url = os.getenv("DOCLING_SERVE_TEST_REDIS_URL", "").strip()
    if not redis_url:
        pytest.skip("DOCLING_SERVE_TEST_REDIS_URL is not configured")
    connection = Redis.from_url(redis_url)
    queue_name = "docling-canonical-validation"
    queue = Queue(queue_name, connection=connection)
    config = RQOrchestratorConfig(
        redis_url=redis_url,
        queue_name=queue_name,
        results_prefix="docling-test-result:",
        results_ttl=30,
        failure_ttl=30,
        scratch_dir=tmp_path,
    )
    task = Task(
        task_id="rq-live-canonical",
        task_type=TaskType.CHUNK,
        sources=[
            FileSource(
                filename="rq-live.md",
                base64_string=base64.b64encode(b"# RQ canonical acceptance").decode(),
            )
        ],
        metadata={
            "tenant_id": "rq-acceptance",
            "canonical_ingestion": {
                "profile": "document",
                "ocr_policy": "auto",
            },
        },
    )

    def store_native_result(
        worker_task,
        _conversion_manager,
        orchestrator_config,
        _scratch_dir,
        **_callbacks,
    ):
        result_key = f"{orchestrator_config.results_prefix}{worker_task.task_id}"
        native = DoclingTaskResult(
            num_converted=1,
            num_succeeded=1,
            num_failed=0,
            processing_time=0.01,
            result=ChunkedDocumentResult(chunks=[], documents=[]),
        )
        connection.setex(
            result_key,
            orchestrator_config.results_ttl,
            msgpack.packb(
                make_msgpack_safe(native.model_dump()),
                use_bin_type=True,
            ),
        )
        return result_key

    monkeypatch.setattr(
        "docling_serve.rq_job_wrapper._run_docling_task",
        store_native_result,
    )
    job = queue.enqueue(
        instrumented_docling_task,
        task.model_dump(),
        object(),
        config,
        tmp_path,
        job_id=task.task_id,
        result_ttl=30,
        failure_ttl=30,
    )

    SimpleWorker([queue], connection=connection).work(burst=True)
    job.refresh()

    result_key = str(job.result)
    packed = connection.get(result_key)
    assert isinstance(packed, bytes)
    stored = DoclingTaskResult.model_validate(msgpack.unpackb(packed, raw=False))
    canonical = canonical_from_task_result(stored)
    assert canonical is not None
    assert canonical.contract == "docling.canonical-ingestion.v1"
    assert canonical.routing.domain == "document"
    assert connection.ttl(result_key) > 0

    connection.delete(result_key)
    job.delete()
    assert connection.exists(result_key, job.key) == 0
