import asyncio
import gc
import logging
import os
from collections import Counter
from typing import Annotated

import psutil
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
    status,
)

from docling.datamodel.service.callbacks import (
    ProgressCallbackRequest,
    ProgressCallbackResponse,
)
from docling.datamodel.service.responses import (
    ChunkDocumentResponse,
    ClearResponse,
    ConvertDocumentResponse,
    MessageKind,
    PresignedUrlConvertDocumentResponse,
    PresignedUrlConvertResponse,
    TaskFailureResult,
    TaskStatusResponse,
    WebsocketMessage,
)
from docling_jobkit.datamodel.stored_outcome import (
    StoredFailureOutcome,
    StoredSuccessOutcome,
)
from docling_jobkit.orchestrators.base_orchestrator import (
    BaseOrchestrator,
    ProgressInvalid,
    RedisBackpressureError,
    TaskNotFoundError,
)

from docling_serve.api.deps import ApiDependencies
from docling_serve.auth import AuthenticationResult
from docling_serve.ingestion.canonical_result import CanonicalTaskResult
from docling_serve.public_errors import build_public_http_detail
from docling_serve.response_preparation import prepare_response
from docling_serve.websocket_notifier import WebsocketNotifier

_log = logging.getLogger(__name__)


def create_tasks_router(deps: ApiDependencies) -> APIRouter:  # noqa: C901
    router = APIRouter()
    require_auth = deps.require_auth
    get_async_orchestrator = deps.orchestrator_provider

    # Task status poll
    @router.get(
        "/v1/status/poll/{task_id}",
        tags=["tasks"],
        response_model=TaskStatusResponse,
    )
    async def task_status_poll(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        task_id: str,
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
        wait: Annotated[
            float,
            Query(description="Number of seconds to wait for a completed status."),
        ] = 0.0,
    ):
        tenant_id = deps.get_tenant_id(x_tenant_id)
        try:
            task = await orchestrator.task_status(task_id=task_id, wait=wait)
            deps.assert_task_tenant(task, tenant_id)
            task_queue_position = await orchestrator.get_queue_position(task_id=task_id)
        except TaskNotFoundError:
            raise HTTPException(status_code=404, detail="Task not found.")
        return TaskStatusResponse(
            task_id=task.task_id,
            task_type=task.task_type,
            task_status=task.task_status,
            task_position=task_queue_position,
            task_meta=task.processing_meta,
            error_message=task.error_message,
            failure=task.failure,
        )

    # Task status websocket
    @router.websocket(
        "/v1/status/ws/{task_id}",
    )
    async def task_status_ws(
        websocket: WebSocket,
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        task_id: str,
        api_key: Annotated[str, Query()] = "",
        tenant_id: Annotated[str | None, Query()] = None,
    ):
        tenant_id = deps.authenticate_status_websocket(
            websocket,
            require_auth,
            api_key,
            tenant_id,
        )

        tenant_id = tenant_id or "default"

        assert isinstance(orchestrator.notifier, WebsocketNotifier)
        await websocket.accept()

        try:
            task = await orchestrator.task_status(task_id=task_id)
            deps.assert_task_tenant(task, tenant_id)
        except RedisBackpressureError:
            await websocket.send_text(
                WebsocketMessage(
                    message=MessageKind.ERROR,
                    error="Server is busy, please try again shortly.",
                ).model_dump_json()
            )
            await websocket.close()
            return
        except TaskNotFoundError:
            await websocket.send_text(
                WebsocketMessage(
                    message=MessageKind.ERROR, error="Task not found."
                ).model_dump_json()
            )
            await websocket.close()
            return

        # Track active WebSocket connections for this job
        orchestrator.notifier.task_subscribers.setdefault(task_id, set()).add(websocket)

        try:
            task_queue_position = await orchestrator.get_queue_position(task_id=task_id)
            task_response = TaskStatusResponse(
                task_id=task.task_id,
                task_type=task.task_type,
                task_status=task.task_status,
                task_position=task_queue_position,
                task_meta=task.processing_meta,
                error_message=task.error_message,
                failure=task.failure,
            )
            await websocket.send_text(
                WebsocketMessage(
                    message=MessageKind.CONNECTION, task=task_response
                ).model_dump_json()
            )
            while True:
                # Refresh from the orchestrator each iteration so the client
                # always sees current state — and the socket is closed on
                # completion — even if the real-time pub/sub push was missed.
                task = await orchestrator.task_status(task_id=task_id)
                task_queue_position = await orchestrator.get_queue_position(
                    task_id=task_id
                )
                task_response = TaskStatusResponse(
                    task_id=task.task_id,
                    task_type=task.task_type,
                    task_status=task.task_status,
                    task_position=task_queue_position,
                    task_meta=task.processing_meta,
                    error_message=task.error_message,
                    failure=task.failure,
                )
                await websocket.send_text(
                    WebsocketMessage(
                        message=MessageKind.UPDATE, task=task_response
                    ).model_dump_json()
                )
                if task.is_completed():
                    await websocket.close()
                    return
                # each client message will be interpreted as a request for update
                msg = await websocket.receive_text()
                _log.debug(f"Received message: {msg}")

        except TaskNotFoundError:
            # Task was removed (e.g. reaped) while streaming; close gracefully.
            try:
                await websocket.close()
            except Exception:
                pass
        except RedisBackpressureError:
            try:
                await websocket.send_text(
                    WebsocketMessage(
                        message=MessageKind.ERROR,
                        error="Server is busy, please try again shortly.",
                    ).model_dump_json()
                )
                await websocket.close()
            except Exception:
                pass
        except WebSocketDisconnect:
            _log.info(f"WebSocket disconnected for job {task_id}")

        finally:
            subs = orchestrator.notifier.task_subscribers.get(task_id)
            if subs:
                subs.discard(websocket)

    # Task result
    @router.get(
        "/v1/result/{task_id}",
        tags=["tasks"],
        response_model=ConvertDocumentResponse
        | PresignedUrlConvertDocumentResponse
        | PresignedUrlConvertResponse
        | ChunkDocumentResponse
        | CanonicalTaskResult
        | TaskFailureResult,
        responses={
            200: {
                "content": {"application/zip": {}},
            }
        },
    )
    async def task_result(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        background_tasks: BackgroundTasks,
        task_id: str,
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        tenant_id = deps.get_tenant_id(x_tenant_id)
        try:
            task = await orchestrator.task_status(task_id=task_id)
            deps.assert_task_tenant(task, tenant_id)
            outcome = await orchestrator.task_outcome(task_id=task_id)
            if outcome is None:
                raise HTTPException(
                    status_code=404,
                    detail="Task result not found. Please wait for a completion status.",
                )
            if isinstance(outcome, StoredFailureOutcome):
                return TaskFailureResult(failure=outcome.failure)
            if isinstance(outcome, StoredSuccessOutcome):
                task_result = outcome.result
            else:
                task_result = outcome
            response = await prepare_response(
                task_id=task_id,
                task_result=task_result,
                orchestrator=orchestrator,
                background_tasks=background_tasks,
            )
            return response
        except TaskNotFoundError:
            raise HTTPException(status_code=404, detail="Task not found.")

    # Update task progress
    @router.post(
        "/v1/callback/task/progress",
        tags=["internal"],
        include_in_schema=False,
        response_model=ProgressCallbackResponse,
    )
    async def callback_task_progress(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        request: ProgressCallbackRequest,
    ):
        try:
            await orchestrator.receive_task_progress(request=request)
            return ProgressCallbackResponse(status="ack")
        except TaskNotFoundError:
            raise HTTPException(status_code=404, detail="Task not found.")
        except ProgressInvalid as err:
            raise HTTPException(
                status_code=400,
                detail=build_public_http_detail(
                    exc=err,
                    debug_enabled=deps.settings.debug_error_details,
                    fallback_message="Invalid progress payload.",
                ),
            )

    #### Clear requests

    # Offload models
    @router.get(
        "/v1/clear/converters",
        tags=["clear"],
        response_model=ClearResponse,
    )
    async def clear_converters(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
    ):
        await orchestrator.clear_converters()
        return ClearResponse()

    # Clean results
    @router.get(
        "/v1/clear/results",
        tags=["clear"],
        response_model=ClearResponse,
    )
    async def clear_results(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        older_than: float = 3600,
    ):
        await orchestrator.clear_results(older_than=older_than)
        return ClearResponse()

    @router.get("/v1/memory/stats", tags=["management"])
    async def memory_stats():
        if not deps.settings.enable_management_endpoints:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden. The server is configured for not showing internal managament details.",
            )
        process = psutil.Process(os.getpid())
        rss_mb = process.memory_info().rss / 1024 / 1024
        stats = {}

        # total memory (this is what triggers OOM); cgroup v2 only
        try:
            with open("/sys/fs/cgroup/memory.current") as f:  # noqa: ASYNC230
                stats["cgroup_total"] = int(f.read()) / 1024 / 1024
        except OSError:
            stats["cgroup_total"] = None

        # detailed breakdown
        try:
            with open("/sys/fs/cgroup/memory.stat") as f:  # noqa: ASYNC230
                for line in f:
                    key, value = line.split()
                    stats[key] = int(value) / 1024 / 1024
        except OSError:
            pass

        return {
            "rss": rss_mb,
            "anon": stats.get("anon", 0.0),
            "file": stats.get("file", 0.0),
            "slab": stats.get("slab", 0.0),
            "cgroup_total": stats.get("cgroup_total"),
        }

    @router.get("/v1/memory/counts", tags=["management"])
    async def memory_counts():
        if not deps.settings.enable_management_endpoints:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden. The server is configured for not showing internal managament details.",
            )
        gc.collect()
        objs = gc.get_objects()
        counter = Counter(type(o).__name__ for o in objs)
        tasks = asyncio.all_tasks()

        return {
            "gc": {
                "counts": gc.get_count(),
                "threshold": gc.get_threshold(),
            },
            "objects": {
                "total": len(objs),
            },
            "asyncio": {
                "all_tasks": len(tasks),
                "pending_tasks": sum(1 for t in tasks if not t.done()),
            },
            "top_types": [{"type": k, "count": v} for k, v in counter.most_common(20)],
        }

    return router
