"""Owned Local orchestrator adapter for tenant and lifecycle parity."""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

from docling.datamodel.service.tasks import TaskType
from docling_jobkit.convert.chunking import process_chunkable_results
from docling_jobkit.convert.results import process_exportable_results
from docling_jobkit.convert.source_expansion import expand_task_sources
from docling_jobkit.datamodel.exportable_document import (
    ExportableDocument,
    source_to_public_uri,
)
from docling_jobkit.datamodel.result import DoclingTaskResult
from docling_jobkit.datamodel.task import Task
from docling_jobkit.datamodel.task_meta import TaskStatus
from docling_jobkit.orchestrators.callback_invoker import CallbackInvoker
from docling_jobkit.orchestrators.local.orchestrator import LocalOrchestrator

from docling_serve.execution.failure_mapping import TaskFailureMapping
from docling_serve.execution.staged_docling_task import run_staged_task
from docling_serve.ingestion.canonical_task import (
    finalize_canonical_task,
    prepare_canonical_task,
)
from docling_serve.legacy_office import (
    _build_source_metadata_chunker_class,
    build_converter_manager,
)
from docling_serve.upload_staging import redact_sensitive_text, staged_identities

_log = logging.getLogger(__name__)


class LegacyOfficeLocalWorker:
    """Local worker using the service manager without module-level replacement."""

    def __init__(self, worker_id: int, orchestrator: Any):
        self.worker_id = worker_id
        self.orchestrator = orchestrator

    async def loop(self) -> None:  # noqa: C901
        if self.orchestrator.config.shared_models:
            manager = self.orchestrator.cm
        else:
            manager = build_converter_manager(self.orchestrator.cm.config)
            self.orchestrator.worker_cms.append(manager)
        while True:
            task_id = await self.orchestrator.task_queue.get()
            self.orchestrator.queue_list.remove(task_id)
            if task_id not in self.orchestrator.tasks:
                raise RuntimeError(f"Task {task_id} not found.")
            task = self.orchestrator.tasks[task_id]
            workdir = self.orchestrator.scratch_dir / task_id
            try:
                task.set_status(TaskStatus.STARTED)
                if self.orchestrator.notifier:
                    await self.orchestrator.notifier.notify_task_subscribers(task_id)
                    await self.orchestrator.notifier.notify_queue_positions()
                callback_invoker = CallbackInvoker() if task.callbacks else None

                def run_task(worker_task: Task) -> DoclingTaskResult:
                    with prepare_canonical_task(worker_task) as canonical:
                        execution_task = (
                            canonical.task if canonical is not None else worker_task
                        )
                        convert_sources, headers = expand_task_sources(
                            execution_task,
                            max_file_size=manager.config.max_file_size,
                        )
                        conv_results = manager.convert_documents(
                            sources=convert_sources,
                            options=execution_task.convert_options,
                            headers=headers,
                        )
                        identities = staged_identities()

                        def public_source_uri(index: int, result: Any) -> str:
                            identity = (
                                identities[index]
                                if identities is not None and index < len(identities)
                                else None
                            )
                            if identity is not None:
                                return identity.original_uri or identity.original_name
                            if index < len(execution_task.sources):
                                public_uri = source_to_public_uri(
                                    execution_task.sources[index]
                                )
                                if public_uri is not None:
                                    return public_uri
                            return str(result.input.file)

                        exportable = (
                            ExportableDocument.from_conversion_result(
                                result,
                                source_index=index,
                                source_uri=public_source_uri(index, result),
                            )
                            for index, result in enumerate(conv_results)
                        )
                        if execution_task.task_type == TaskType.CONVERT:
                            result = process_exportable_results(
                                task=execution_task,
                                exportable_documents=exportable,
                                work_dir=workdir,
                                s3_presigned_config=self.orchestrator.config.s3_presigned_config,
                                callback_invoker=callback_invoker,
                            )
                        elif execution_task.task_type == TaskType.CHUNK:
                            result = process_chunkable_results(
                                task=execution_task,
                                exportable_documents=exportable,
                                work_dir=workdir,
                                chunker_manager=self.orchestrator.chunker_manager,
                                callback_invoker=callback_invoker,
                            )
                        else:
                            raise RuntimeError(
                                f"Unsupported task type: {execution_task.task_type}"
                            )
                        return finalize_canonical_task(canonical, result)

                def map_failure(
                    exc: Exception,
                    failure: TaskFailureMapping,
                ) -> None:
                    _log.error(
                        "Local worker failed task %s: %s",
                        task_id,
                        redact_sensitive_text(str(exc)),
                    )
                    task.set_status(TaskStatus.FAILURE)
                    task.error_message = failure.public_message

                def report_cleanup_failure(_exc: Exception) -> None:
                    _log.error(
                        "Staged input cleanup requires lifecycle reconciliation "
                        "for task %s",
                        task_id,
                    )

                self.orchestrator._task_results[task_id] = await asyncio.to_thread(
                    run_staged_task,
                    task,
                    run_task,
                    on_failure=map_failure,
                    on_cleanup_failure=report_cleanup_failure,
                )
                task.sources = []
                task.set_status(TaskStatus.SUCCESS)
            except Exception as exc:
                if task.task_status != TaskStatus.FAILURE:
                    _log.error(
                        "Local worker failed task %s: %s",
                        task_id,
                        redact_sensitive_text(str(exc)),
                    )
                    task.set_status(TaskStatus.FAILURE)
            finally:
                if workdir.exists():
                    shutil.rmtree(workdir)
                if self.orchestrator.notifier:
                    await self.orchestrator.notifier.notify_task_subscribers(task_id)
                self.orchestrator.task_queue.task_done()


class DoclingServeLocalOrchestrator(LocalOrchestrator):
    """Retain task metadata and deterministically stop local workers."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.chunker_manager = _build_source_metadata_chunker_class()()

    async def enqueue(
        self,
        *args: Any,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Task:
        kwargs["metadata"] = metadata
        task = await super().enqueue(*args, **kwargs)
        task.metadata = dict(metadata or {})
        return task

    async def process_queue(self) -> None:
        workers = [
            asyncio.create_task(LegacyOfficeLocalWorker(worker_id, self).loop())
            for worker_id in range(self.config.num_workers)
        ]
        try:
            await asyncio.gather(*workers)
        finally:
            for worker in workers:
                worker.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def task_status(self, task_id: str, wait: float = 0.0) -> Task:
        return await super().task_status(task_id, wait)
