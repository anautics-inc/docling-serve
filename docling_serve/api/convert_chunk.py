import logging
from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    Header,
    HTTPException,
    UploadFile,
    status,
)

from docling.datamodel.service.chunking import (
    HierarchicalChunkerOptions,
    HybridChunkerOptions,
)
from docling.datamodel.service.options import (
    ConvertDocumentsOptions as ConvertDocumentsRequestOptions,
)
from docling.datamodel.service.requests import (
    BatchConvertSourcesRequest,
    TargetName,
    make_request_model,
)
from docling.datamodel.service.responses import (
    ChunkDocumentResponse,
    ConvertDocumentResponse,
    PresignedUrlConvertDocumentResponse,
    PresignedUrlConvertResponse,
    TaskStatusResponse,
)
from docling.datamodel.service.targets import InBodyTarget, ZipTarget
from docling.datamodel.service.tasks import TaskType
from docling_jobkit.datamodel.chunking import ChunkingExportOptions
from docling_jobkit.orchestrators.base_orchestrator import (
    BaseOrchestrator,
)

from docling_serve.api.deps import ApiDependencies
from docling_serve.auth import AuthenticationResult
from docling_serve.helper_functions import FormDepends
from docling_serve.response_preparation import prepare_response

_log = logging.getLogger(__name__)


def create_convert_chunk_router(deps: ApiDependencies) -> APIRouter:  # noqa: C901
    router = APIRouter()
    require_auth = deps.require_auth
    ConvertSourcesRequestModel = deps.convert_sources_request_model
    default_target_name = deps.default_target_name
    get_async_orchestrator = deps.orchestrator_provider

    # Convert a document from URL(s)
    @router.post(
        "/v1/convert/source",
        tags=["convert"],
        response_model=ConvertDocumentResponse
        | PresignedUrlConvertDocumentResponse
        | PresignedUrlConvertResponse,
        responses={
            200: {
                "content": {"application/zip": {}},
                # "description": "Return the JSON item or an image.",
            }
        },
    )
    async def process_url(
        background_tasks: BackgroundTasks,
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        # FastAPI requires the concrete runtime model in the route signature;
        # mypy cannot treat a factory/dependency-provided class as a type.
        conversion_request: ConvertSourcesRequestModel,  # type: ignore[valid-type]
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        prepared_request = deps.prepare_convert_request(conversion_request)
        tenant_id = deps.get_tenant_id(x_tenant_id)
        _log.info("[TENANT_ID] process_url received tenant scope")
        task = await deps.enqueue_source(
            orchestrator=orchestrator, request=prepared_request, tenant_id=tenant_id
        )
        completed = await deps.wait_task_complete(
            orchestrator=orchestrator, task_id=task.task_id
        )

        if not completed:
            try:
                await orchestrator.delete_task(task_id=task.task_id)
            except Exception:
                _log.warning(
                    "Failed to abort timed-out task %s", task.task_id, exc_info=True
                )
            raise HTTPException(
                status_code=504,
                detail=f"Conversion is taking too long. The maximum wait time is configure as DOCLING_SERVE_MAX_SYNC_WAIT={deps.settings.max_sync_wait}.",
            )

        task_result = await orchestrator.task_result(task_id=task.task_id)
        if task_result is None:
            raise HTTPException(
                status_code=404,
                detail="Task result not found. Please wait for a completion status.",
            )
        response = await prepare_response(
            task_id=task.task_id,
            task_result=task_result,
            orchestrator=orchestrator,
            background_tasks=background_tasks,
        )
        return response

    # Convert a document from file(s)
    @router.post(
        "/v1/convert/file",
        tags=["convert"],
        response_model=ConvertDocumentResponse
        | PresignedUrlConvertDocumentResponse
        | PresignedUrlConvertResponse,
        responses={
            200: {
                "content": {"application/zip": {}},
            }
        },
    )
    async def process_file(
        background_tasks: BackgroundTasks,
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        files: list[UploadFile],
        options: Annotated[
            ConvertDocumentsRequestOptions, FormDepends(ConvertDocumentsRequestOptions)
        ],
        target_type: Annotated[TargetName, Form()] = default_target_name,
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        deps.check_file_upload(files, target_type)
        options = deps.prepare_convert_options(options)
        deps.validate_multipart_target_type(target_type)
        tenant_id = deps.get_tenant_id(x_tenant_id)
        _log.info("[TENANT_ID] process_file received tenant scope")
        target = deps.resolve_file_target(target_type)
        task = await deps.enqueue_file(
            task_type=TaskType.CONVERT,
            orchestrator=orchestrator,
            files=files,
            convert_options=options,
            chunking_options=None,
            chunking_export_options=None,
            target=target,
            callbacks=[],
            tenant_id=tenant_id,
        )
        completed = await deps.wait_task_complete(
            orchestrator=orchestrator, task_id=task.task_id
        )

        if not completed:
            try:
                await orchestrator.delete_task(task_id=task.task_id)
            except Exception:
                _log.warning(
                    "Failed to abort timed-out task %s", task.task_id, exc_info=True
                )
            raise HTTPException(
                status_code=504,
                detail=f"Conversion is taking too long. The maximum wait time is configure as DOCLING_SERVE_MAX_SYNC_WAIT={deps.settings.max_sync_wait}.",
            )

        task_result = await orchestrator.task_result(task_id=task.task_id)
        if task_result is None:
            raise HTTPException(
                status_code=404,
                detail="Task result not found. Please wait for a completion status.",
            )
        response = await prepare_response(
            task_id=task.task_id,
            task_result=task_result,
            orchestrator=orchestrator,
            background_tasks=background_tasks,
        )
        return response

    # Convert a document from URL(s) using the async api
    @router.post(
        "/v1/convert/source/async",
        tags=["convert"],
        response_model=TaskStatusResponse,
    )
    async def process_url_async(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        # See process_url: this annotation is consumed dynamically by FastAPI.
        conversion_request: ConvertSourcesRequestModel,  # type: ignore[valid-type]
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        prepared_request = deps.prepare_convert_request(conversion_request)
        tenant_id = deps.get_tenant_id(x_tenant_id)
        _log.info("[TENANT_ID] process_url_async received tenant scope")
        task = await deps.enqueue_source(
            orchestrator=orchestrator, request=prepared_request, tenant_id=tenant_id
        )
        task_queue_position = await orchestrator.get_queue_position(
            task_id=task.task_id
        )
        return TaskStatusResponse(
            task_id=task.task_id,
            task_type=task.task_type,
            task_status=task.task_status,
            task_position=task_queue_position,
            task_meta=task.processing_meta,
            error_message=task.error_message,
            failure=task.failure,
        )

    @router.post(
        "/v1/convert/source/batch",
        tags=["convert"],
        response_model=TaskStatusResponse,
    )
    async def process_source_batch(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        conversion_request: BatchConvertSourcesRequest,
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        conversion_request = deps.prepare_batch_convert_request(conversion_request)
        tenant_id = deps.get_tenant_id(x_tenant_id)
        _log.info("[TENANT_ID] process_source_batch received tenant scope")
        task = await deps.enqueue_source(
            orchestrator=orchestrator,
            request=conversion_request,
            tenant_id=tenant_id,
        )
        task_queue_position = await orchestrator.get_queue_position(
            task_id=task.task_id
        )
        return TaskStatusResponse(
            task_id=task.task_id,
            task_type=task.task_type,
            task_status=task.task_status,
            task_position=task_queue_position,
            task_meta=task.processing_meta,
            error_message=task.error_message,
            failure=task.failure,
        )

    # Convert a document from file(s) using the async api
    @router.post(
        "/v1/convert/file/async",
        tags=["convert"],
        response_model=TaskStatusResponse,
    )
    async def process_file_async(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        background_tasks: BackgroundTasks,
        files: list[UploadFile],
        options: Annotated[
            ConvertDocumentsRequestOptions, FormDepends(ConvertDocumentsRequestOptions)
        ],
        target_type: Annotated[TargetName, Form()] = default_target_name,
        x_tenant_id: Annotated[
            str | None, Header(alias=deps.settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        deps.check_file_upload(files, target_type)
        options = deps.prepare_convert_options(options)
        deps.validate_multipart_target_type(target_type)
        tenant_id = deps.get_tenant_id(x_tenant_id)
        _log.info("[TENANT_ID] process_file_async received tenant scope")
        target = deps.resolve_file_target(target_type)
        task = await deps.enqueue_file(
            task_type=TaskType.CONVERT,
            orchestrator=orchestrator,
            files=files,
            convert_options=options,
            chunking_options=None,
            chunking_export_options=None,
            target=target,
            callbacks=[],
            tenant_id=tenant_id,
        )
        task_queue_position = await orchestrator.get_queue_position(
            task_id=task.task_id
        )
        return TaskStatusResponse(
            task_id=task.task_id,
            task_type=task.task_type,
            task_status=task.task_status,
            task_position=task_queue_position,
            task_meta=task.processing_meta,
            error_message=task.error_message,
            failure=task.failure,
        )

    # Chunking endpoints
    for display_name, path_name, opt_cls in (
        ("HybridChunker", "hybrid", HybridChunkerOptions),
        ("HierarchicalChunker", "hierarchical", HierarchicalChunkerOptions),
    ):
        req_cls = make_request_model(opt_cls)

        @router.post(
            f"/v1/chunk/{path_name}/source/async",
            name=f"Chunk sources with {display_name} as async task",
            tags=["chunk"],
            response_model=TaskStatusResponse,
        )
        async def chunk_source_async(
            background_tasks: BackgroundTasks,
            auth: Annotated[AuthenticationResult, Depends(require_auth)],
            orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
            request: req_cls,  # type: ignore[valid-type]  # FastAPI runtime model
            x_tenant_id: Annotated[
                str | None,
                Header(alias=deps.settings.eng_ray_tenant_id_header),
            ] = None,
        ):
            request = deps.prepare_chunk_request(request)
            tenant_id = deps.get_tenant_id(x_tenant_id)
            _log.info(
                "[TENANT_ID] chunk_source_async (%s) received tenant scope",
                path_name,
            )
            task = await deps.enqueue_source(
                orchestrator=orchestrator, request=request, tenant_id=tenant_id
            )
            task_queue_position = await orchestrator.get_queue_position(
                task_id=task.task_id
            )
            return TaskStatusResponse(
                task_id=task.task_id,
                task_type=task.task_type,
                task_status=task.task_status,
                task_position=task_queue_position,
                task_meta=task.processing_meta,
                error_message=task.error_message,
                failure=task.failure,
            )

        @router.post(
            f"/v1/chunk/{path_name}/file/async",
            name=f"Chunk files with {display_name} as async task",
            tags=["chunk"],
            response_model=TaskStatusResponse,
        )
        async def chunk_file_async(
            background_tasks: BackgroundTasks,
            auth: Annotated[AuthenticationResult, Depends(require_auth)],
            orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
            files: list[UploadFile],
            convert_options: Annotated[
                ConvertDocumentsRequestOptions,
                FormDepends(
                    ConvertDocumentsRequestOptions,
                    prefix="convert_",
                    excluded_fields=[
                        "to_formats",
                    ],
                ),
            ],
            # FastAPI consumes this loop-selected form model at route registration.
            chunking_options: Annotated[  # type: ignore[valid-type]
                opt_cls,
                FormDepends(
                    opt_cls,
                    prefix="chunking_",
                    excluded_fields=["chunker"],
                ),
            ],
            include_converted_doc: Annotated[
                bool,
                Form(
                    description="If true, the output will include both the chunks and the converted document."
                ),
            ] = False,
            canonical: Annotated[
                bool,
                Form(
                    description="Return the format-neutral canonical ingestion envelope."
                ),
            ] = False,
            profile: Annotated[str, Form()] = "auto",
            ocr_policy: Annotated[str, Form()] = "auto",
            prefix: Annotated[str, Form()] = "",
            bucket: Annotated[str, Form()] = "",
            target_type: Annotated[
                TargetName,
                Form(description="Specification for the type of output target."),
            ] = TargetName.INBODY,
            x_tenant_id: Annotated[
                str | None,
                Header(alias=deps.settings.eng_ray_tenant_id_header),
            ] = None,
        ):
            if target_type == TargetName.PRESIGNED_URL:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="presigned_url target is not supported for chunk endpoints.",
                )
            convert_options = deps.prepare_convert_options(convert_options)
            deps.validate_multipart_target_type(target_type)
            tenant_id = deps.get_tenant_id(x_tenant_id)
            _log.info(
                "[TENANT_ID] chunk_file_async (%s) received tenant scope",
                path_name,
            )
            target = InBodyTarget() if target_type == TargetName.INBODY else ZipTarget()
            task = await deps.enqueue_file(
                task_type=TaskType.CHUNK,
                orchestrator=orchestrator,
                files=files,
                convert_options=convert_options,
                chunking_options=chunking_options,
                chunking_export_options=ChunkingExportOptions(
                    include_converted_doc=include_converted_doc
                ),
                target=target,
                callbacks=[],
                tenant_id=tenant_id,
                task_metadata={
                    "canonical_ingestion": {
                        "profile": profile,
                        "ocr_policy": ocr_policy,
                        "prefix": prefix,
                        "bucket": bucket,
                    }
                }
                if canonical
                else None,
            )
            task_queue_position = await orchestrator.get_queue_position(
                task_id=task.task_id
            )
            return TaskStatusResponse(
                task_id=task.task_id,
                task_type=task.task_type,
                task_status=task.task_status,
                task_position=task_queue_position,
                task_meta=task.processing_meta,
                error_message=task.error_message,
                failure=task.failure,
            )

        @router.post(
            f"/v1/chunk/{path_name}/source",
            name=f"Chunk sources with {display_name}",
            tags=["chunk"],
            response_model=ChunkDocumentResponse,
            responses={
                200: {
                    "content": {"application/zip": {}},
                    # "description": "Return the JSON item or an image.",
                }
            },
        )
        async def chunk_source(
            background_tasks: BackgroundTasks,
            auth: Annotated[AuthenticationResult, Depends(require_auth)],
            orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
            request: req_cls,  # type: ignore[valid-type]  # FastAPI runtime model
            x_tenant_id: Annotated[
                str | None,
                Header(alias=deps.settings.eng_ray_tenant_id_header),
            ] = None,
        ):
            request = deps.prepare_chunk_request(request)
            tenant_id = deps.get_tenant_id(x_tenant_id)
            _log.info(
                "[TENANT_ID] chunk_source (%s) received tenant scope",
                path_name,
            )
            task = await deps.enqueue_source(
                orchestrator=orchestrator, request=request, tenant_id=tenant_id
            )
            completed = await deps.wait_task_complete(
                orchestrator=orchestrator, task_id=task.task_id
            )

            if not completed:
                try:
                    await orchestrator.delete_task(task_id=task.task_id)
                except Exception:
                    _log.warning(
                        "Failed to abort timed-out task %s",
                        task.task_id,
                        exc_info=True,
                    )
                raise HTTPException(
                    status_code=504,
                    detail=f"Conversion is taking too long. The maximum wait time is configure as DOCLING_SERVE_MAX_SYNC_WAIT={deps.settings.max_sync_wait}.",
                )

            task_result = await orchestrator.task_result(task_id=task.task_id)
            if task_result is None:
                raise HTTPException(
                    status_code=404,
                    detail="Task result not found. Please wait for a completion status.",
                )
            response = await prepare_response(
                task_id=task.task_id,
                task_result=task_result,
                orchestrator=orchestrator,
                background_tasks=background_tasks,
            )
            return response

        @router.post(
            f"/v1/chunk/{path_name}/file",
            name=f"Chunk files with {display_name}",
            tags=["chunk"],
            response_model=ChunkDocumentResponse,
            responses={
                200: {
                    "content": {"application/zip": {}},
                }
            },
        )
        async def chunk_file(
            background_tasks: BackgroundTasks,
            auth: Annotated[AuthenticationResult, Depends(require_auth)],
            orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
            files: list[UploadFile],
            convert_options: Annotated[
                ConvertDocumentsRequestOptions,
                FormDepends(
                    ConvertDocumentsRequestOptions,
                    prefix="convert_",
                    excluded_fields=[
                        "to_formats",
                    ],
                ),
            ],
            # FastAPI consumes this loop-selected form model at route registration.
            chunking_options: Annotated[  # type: ignore[valid-type]
                opt_cls,
                FormDepends(
                    opt_cls,
                    prefix="chunking_",
                    excluded_fields=["chunker"],
                ),
            ],
            include_converted_doc: Annotated[
                bool,
                Form(
                    description="If true, the output will include both the chunks and the converted document."
                ),
            ] = False,
            target_type: Annotated[
                TargetName,
                Form(description="Specification for the type of output target."),
            ] = TargetName.INBODY,
            x_tenant_id: Annotated[
                str | None,
                Header(alias=deps.settings.eng_ray_tenant_id_header),
            ] = None,
        ):
            if target_type == TargetName.PRESIGNED_URL:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="presigned_url target is not supported for chunk endpoints.",
                )
            convert_options = deps.prepare_convert_options(convert_options)
            deps.validate_multipart_target_type(target_type)
            tenant_id = deps.get_tenant_id(x_tenant_id)
            _log.info(
                "[TENANT_ID] chunk_file (%s) received tenant scope",
                path_name,
            )
            target = InBodyTarget() if target_type == TargetName.INBODY else ZipTarget()
            task = await deps.enqueue_file(
                task_type=TaskType.CHUNK,
                orchestrator=orchestrator,
                files=files,
                convert_options=convert_options,
                chunking_options=chunking_options,
                chunking_export_options=ChunkingExportOptions(
                    include_converted_doc=include_converted_doc
                ),
                target=target,
                callbacks=[],
                tenant_id=tenant_id,
            )
            completed = await deps.wait_task_complete(
                orchestrator=orchestrator, task_id=task.task_id
            )

            if not completed:
                try:
                    await orchestrator.delete_task(task_id=task.task_id)
                except Exception:
                    _log.warning(
                        "Failed to abort timed-out task %s",
                        task.task_id,
                        exc_info=True,
                    )
                raise HTTPException(
                    status_code=504,
                    detail=f"Conversion is taking too long. The maximum wait time is configure as DOCLING_SERVE_MAX_SYNC_WAIT={deps.settings.max_sync_wait}.",
                )

            task_result = await orchestrator.task_result(task_id=task.task_id)
            if task_result is None:
                raise HTTPException(
                    status_code=404,
                    detail="Task result not found. Please wait for a completion status.",
                )
            response = await prepare_response(
                task_id=task.task_id,
                task_result=task_result,
                orchestrator=orchestrator,
                background_tasks=background_tasks,
            )
            return response

    return router
