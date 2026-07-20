"""One staged-input lifecycle shared by Local, RQ, and Ray execution."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import TypeVar

from docling_jobkit.datamodel.task import Task

from docling_serve.execution.failure_mapping import (
    TaskFailureMapping,
    map_task_failure,
)
from docling_serve.upload_staging import (
    StagedUploadRef,
    UploadStagingError,
    cleanup_task_staged_uploads,
    cleanup_task_staged_uploads_sync,
    materialize_staged_task,
    staged_refs_for_task,
)

ResultT = TypeVar("ResultT")
FailureHandler = Callable[[Exception, TaskFailureMapping], None]
CleanupHandler = Callable[[list[StagedUploadRef]], None]
CleanupFailureHandler = Callable[[UploadStagingError], None]


def _notify(
    handler: Callable[..., object] | None, *args: object
) -> object | Awaitable[object] | None:
    return handler(*args) if handler is not None else None


async def _await_result(result: object | Awaitable[object] | None) -> object | None:
    return await result if inspect.isawaitable(result) else result


def run_staged_task(
    task: Task,
    run: Callable[[Task], ResultT],
    *,
    on_failure: FailureHandler | None = None,
    on_cleanup: CleanupHandler | None = None,
    on_cleanup_failure: CleanupFailureHandler | None = None,
) -> ResultT:
    """Materialize, execute, map failures, and clean up a task synchronously."""

    try:
        with materialize_staged_task(task) as worker_task:
            return run(worker_task)
    except Exception as exc:
        _notify(on_failure, exc, map_task_failure(exc))
        raise
    finally:
        try:
            cleanup_task_staged_uploads_sync(task)
        except UploadStagingError as cleanup_exc:
            _notify(on_cleanup_failure, cleanup_exc)
        _notify(on_cleanup, staged_refs_for_task(task))


async def run_staged_task_async(
    task: Task,
    run: Callable[[Task], ResultT | Awaitable[ResultT]],
    *,
    on_failure: (
        Callable[[Exception, TaskFailureMapping], None | Awaitable[None]] | None
    ) = None,
    on_cleanup: (
        Callable[[list[StagedUploadRef]], None | Awaitable[None]] | None
    ) = None,
    on_cleanup_failure: (
        Callable[[UploadStagingError], None | Awaitable[None]] | None
    ) = None,
) -> ResultT:
    """Materialize, execute, map failures, and clean up a task asynchronously."""

    try:
        with materialize_staged_task(task) as worker_task:
            result = run(worker_task)
            if inspect.isawaitable(result):
                return await result
            return result
    except Exception as exc:
        await _await_result(_notify(on_failure, exc, map_task_failure(exc)))
        raise
    finally:
        try:
            await cleanup_task_staged_uploads(task)
        except UploadStagingError as cleanup_exc:
            await _await_result(_notify(on_cleanup_failure, cleanup_exc))
        await _await_result(_notify(on_cleanup, staged_refs_for_task(task)))
