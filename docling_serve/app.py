import asyncio
import copy
import gc
import hashlib
import importlib.metadata
import logging
import os
import shutil
import time
from collections import Counter
from contextlib import asynccontextmanager
from io import BytesIO
from typing import Annotated

import psutil
from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import create_model
from scalar_fastapi import get_scalar_api_reference

from docling.datamodel.base_models import DocumentStream
from docling.datamodel.service.callbacks import (
    CallbackSpec,
    ProgressCallbackRequest,
    ProgressCallbackResponse,
)
from docling.datamodel.service.chunking import (
    BaseChunkerOptions,
    HierarchicalChunkerOptions,
    HybridChunkerOptions,
)
from docling.datamodel.service.options import (
    ConvertDocumentsOptions as ConvertDocumentsRequestOptions,
)
from docling.datamodel.service.requests import (
    BatchConvertSourcesRequest,
    ConvertSourcesRequest,
    FileSourceRequest,
    GenericChunkDocumentsRequest,
    S3SourceRequest,
    TargetName,
    TargetRequest,
    make_request_model,
)
from docling.datamodel.service.responses import (
    ChunkDocumentResponse,
    ClearResponse,
    ConvertDocumentResponse,
    HealthCheckResponse,
    MessageKind,
    PresignedUrlConvertDocumentResponse,
    PresignedUrlConvertResponse,
    ReadinessResponse,
    TaskFailureResult,
    TaskStatusResponse,
    WebsocketMessage,
)
from docling.datamodel.service.sources import FileSource, HttpSource, S3Coordinates
from docling.datamodel.service.targets import (
    InBodyTarget,
    PresignedUrlTarget,
    ZipTarget,
)
from docling.datamodel.service.tasks import TaskType
from docling_jobkit.datamodel.chunking import ChunkingExportOptions
from docling_jobkit.datamodel.stored_outcome import (
    StoredFailureOutcome,
    StoredSuccessOutcome,
)
from docling_jobkit.datamodel.task import Task, TaskSource
from docling_jobkit.orchestrators.base_orchestrator import (
    BaseOrchestrator,
    ProgressInvalid,
    RedisBackpressureError,
    TaskNotFoundError,
)
from docling_jobkit.orchestrators.rq.orchestrator import RQOrchestrator

from docling_serve.auth import APIKeyAuth, AuthenticationResult
from docling_serve.graph import (
    GraphExtractionUnavailable,
    GraphExtractRequest,
    GraphExtractResponse,
    graph_payload_from_text,
    resolve_profile_template,
)
from docling_serve.helper_functions import DOCLING_VERSIONS, FormDepends
from docling_serve.logging_config import setup_logging
from docling_serve.orchestrator_factory import get_async_orchestrator
from docling_serve.otel_instrumentation import (
    get_metrics_endpoint_content,
    setup_otel_instrumentation,
)
from docling_serve.policy import (
    build_service_policy,
    normalize_convert_options,
    normalize_request,
    resolve_default_target,
    validate_batch_convert_request,
    validate_chunk_request,
    validate_convert_options,
    validate_convert_request,
    validate_target_kind,
)
from docling_serve.public_errors import build_public_http_detail
from docling_serve.response_preparation import prepare_response
from docling_serve.settings import AsyncEngine, docling_serve_settings
from docling_serve.storage import get_scratch
from docling_serve.websocket_notifier import WebsocketNotifier

# Pre-import OCR backends that use cysignals (signal handlers must be registered
# in the main thread; worker threads would raise "signal only works in main thread").
try:
    import tesserocr  # noqa: F401
except (ImportError, Exception):
    pass


# Configure logging based on settings
# This will be called early, but can be reconfigured in __main__.py
log_level = (
    docling_serve_settings.log_level.value
    if docling_serve_settings.log_level
    else "INFO"
)
setup_logging(
    log_format=docling_serve_settings.log_format.value,
    log_level=log_level,
    header_prefix=docling_serve_settings.log_header_prefix,
)

_log = logging.getLogger(__name__)

# Tracks whether warm_up_caches() has completed.  Meaningful only for the
# LocalOrchestrator (which eagerly loads ML models); the RQ orchestrator's
# implementation is a no-op so this event fires instantly in RQ deployments.
_models_ready = asyncio.Event()

# Set if the background queue processor task dies with an error. Liveness/
# readiness then fail so the platform restarts the pod instead of silently
# serving with a dead orchestrator loop: a dead pub/sub listener stops WebSocket
# push delivery while polling still succeeds, which is otherwise very hard to
# detect.
_queue_processor_failed = asyncio.Event()


def _supervise_queue_processor(task: asyncio.Task, failed_event: asyncio.Event) -> None:
    """Mark the orchestrator loop unhealthy only if it died with an exception.

    A clean return is legitimate: some engines (e.g. KFP) have no in-process
    queue loop and ``process_queue()`` is a no-op, so completing is expected and
    must not flag the pod unhealthy. Only an unhandled exception means a
    supervised loop (RQ/Ray pub/sub listener, Local workers) actually broke.
    """
    if task.cancelled():
        return  # expected on shutdown
    exc = task.exception()
    if exc is None:
        _log.debug("Background queue processor completed without error")
        return
    _log.error("Background queue processor died: %s", exc, exc_info=exc)
    failed_event.set()


# Context manager to initialize and clean up the lifespan of the FastAPI app
@asynccontextmanager
async def lifespan(app: FastAPI):
    scratch_dir = get_scratch()

    orchestrator = get_async_orchestrator()
    notifier = WebsocketNotifier(orchestrator)
    orchestrator.bind_notifier(notifier)

    # Warm up processing cache (loads ML models for LocalOrchestrator;
    # no-op for RQOrchestrator since models live in the worker pods).
    if docling_serve_settings.load_models_at_boot:
        await orchestrator.warm_up_caches()

    _models_ready.set()

    # Start the background queue processor. If a supervised loop (RQ/Ray pub/sub
    # listener, Local workers) ever crashes, the done-callback flags the pod
    # unhealthy so it gets restarted instead of silently dropping WebSocket push.
    queue_task = asyncio.create_task(orchestrator.process_queue())
    queue_task.add_done_callback(
        lambda task: _supervise_queue_processor(task, _queue_processor_failed)
    )

    reaper_task = None
    if isinstance(orchestrator, RQOrchestrator):
        reaper_task = asyncio.create_task(orchestrator._reap_zombie_tasks())

    yield

    # Cancel the background queue processor on shutdown
    queue_task.cancel()
    if reaper_task:
        reaper_task.cancel()
    try:
        await queue_task
    except asyncio.CancelledError:
        _log.info("Queue processor cancelled.")
    if reaper_task:
        try:
            await reaper_task
        except asyncio.CancelledError:
            _log.info("Zombie reaper cancelled.")

    # Remove scratch directory in case it was a tempfile
    if docling_serve_settings.scratch_path is not None:
        shutil.rmtree(scratch_dir, ignore_errors=True)


##################################
# App creation and configuration #
##################################


def create_app():  # noqa: C901
    try:
        version = importlib.metadata.version("docling_serve")
    except importlib.metadata.PackageNotFoundError:
        _log.warning("Unable to get docling_serve version, falling back to 0.0.0")

        version = "0.0.0"

    offline_docs_assets = False
    if (
        docling_serve_settings.static_path is not None
        and (docling_serve_settings.static_path).is_dir()
    ):
        offline_docs_assets = True
        _log.info("Found static assets.")

    require_auth = APIKeyAuth(docling_serve_settings.api_key)
    service_policy = build_service_policy(docling_serve_settings)

    # Clients omit fields left at their model default, so the imported request
    # model's `target` default must match what this deployment actually accepts;
    # otherwise an omitted target arrives carrying a target kind the policy
    # rejects with a spurious 422. Subclass the imported model to repopulate the
    # `target` default with the deployment-resolved one. Because the default is
    # a concrete value it also flows into the OpenAPI schema automatically. The
    # subclass keeps the public "ConvertSourcesRequest" schema name.
    default_target = resolve_default_target(service_policy)
    default_target_name = TargetName(default_target.kind)
    ConvertSourcesRequestModel = create_model(
        "ConvertSourcesRequest",
        __base__=ConvertSourcesRequest,
        target=(TargetRequest, default_target),
    )

    app = FastAPI(
        title="Docling Serve",
        docs_url=None if offline_docs_assets else "/swagger",
        redoc_url=None if offline_docs_assets else "/docs",
        lifespan=lifespan,
        version=version,
    )

    @app.exception_handler(RedisBackpressureError)
    async def redis_backpressure_error_handler(
        request: Request, exc: RedisBackpressureError
    ) -> JSONResponse:
        del request, exc
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "Server is busy, please try again shortly."},
            headers={"Retry-After": "1"},
        )

    if docling_serve_settings.eng_kind == AsyncEngine.RAY:
        from docling_jobkit.orchestrators.ray.orchestrator import (
            DispatcherUnavailableError,
        )

        @app.exception_handler(DispatcherUnavailableError)
        async def dispatcher_unavailable_error_handler(
            request: Request, exc: Exception
        ) -> JSONResponse:
            del request
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "detail": build_public_http_detail(
                        exc=exc,
                        debug_enabled=docling_serve_settings.debug_error_details,
                        fallback_message="Ray dispatcher is unavailable.",
                    )
                },
                headers={"Retry-After": "1"},
            )

    # Setup OpenTelemetry instrumentation
    redis_url = (
        docling_serve_settings.eng_rq_redis_url
        if docling_serve_settings.eng_kind == AsyncEngine.RQ
        else None
    )

    # Get Ray redis_manager if using Ray engine
    ray_redis_manager = None
    if docling_serve_settings.eng_kind == AsyncEngine.RAY:
        from docling_jobkit.orchestrators.ray.orchestrator import RayOrchestrator

        orchestrator = get_async_orchestrator()
        assert isinstance(orchestrator, RayOrchestrator)
        ray_redis_manager = orchestrator.redis_manager

    setup_otel_instrumentation(
        app,
        service_name=docling_serve_settings.otel_service_name,
        enable_metrics=docling_serve_settings.otel_enable_metrics,
        enable_traces=docling_serve_settings.otel_enable_traces,
        enable_prometheus=docling_serve_settings.otel_enable_prometheus,
        enable_otlp_metrics=docling_serve_settings.otel_enable_otlp_metrics,
        redis_url=redis_url,
        metrics_port=docling_serve_settings.metrics_port,
        ray_redis_manager=ray_redis_manager,
    )

    # Add log context middleware to extract request headers
    from docling_serve.logging_config import LogContextMiddleware

    app.add_middleware(
        LogContextMiddleware,
        header_prefix=docling_serve_settings.log_header_prefix,
    )

    origins = docling_serve_settings.cors_origins
    methods = docling_serve_settings.cors_methods
    headers = docling_serve_settings.cors_headers

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=methods,
        allow_headers=headers,
    )

    # Mount the Gradio app
    if docling_serve_settings.enable_ui:
        try:
            import gradio as gr

            from docling_serve.gradio_ui import ui as gradio_ui
            from docling_serve.settings import uvicorn_settings

            tmp_output_dir = get_scratch() / "gradio"
            tmp_output_dir.mkdir(exist_ok=True, parents=True)
            gradio_ui.gradio_output_dir = tmp_output_dir

            # Build the root_path for Gradio, accounting for UVICORN_ROOT_PATH
            gradio_root_path = (
                f"{uvicorn_settings.root_path}/ui"
                if uvicorn_settings.root_path
                else "/ui"
            )

            app = gr.mount_gradio_app(
                app,
                gradio_ui,
                path="/ui",
                allowed_paths=["./logo.png", tmp_output_dir],
                root_path=gradio_root_path,
            )
        except ImportError:
            _log.warning(
                "Docling Serve enable_ui is activated, but gradio is not installed. "
                "Install it with `pip install docling-serve[ui]` "
                "or `pip install gradio`"
            )

    #############################
    # Offline assets definition #
    #############################
    if offline_docs_assets:
        app.mount(
            "/static",
            StaticFiles(directory=docling_serve_settings.static_path),
            name="static",
        )

        @app.get("/swagger", include_in_schema=False)
        async def custom_swagger_ui_html():
            return get_swagger_ui_html(
                openapi_url=app.openapi_url,
                title=app.title + " - Swagger UI",
                oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
                swagger_js_url="/static/swagger-ui-bundle.js",
                swagger_css_url="/static/swagger-ui.css",
            )

        @app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
        async def swagger_ui_redirect():
            return get_swagger_ui_oauth2_redirect_html()

        @app.get("/docs", include_in_schema=False)
        async def redoc_html():
            return get_redoc_html(
                openapi_url=app.openapi_url,
                title=app.title + " - ReDoc",
                redoc_js_url="/static/redoc.standalone.js",
            )

    @app.get("/scalar", include_in_schema=False)
    async def scalar_html():
        return get_scalar_api_reference(
            openapi_url=app.openapi_url,
            title=app.title,
            scalar_favicon_url="https://raw.githubusercontent.com/docling-project/docling/refs/heads/main/docs/assets/logo.svg",
            # hide_client_button=True,  # not yet released but in main
        )

    ########################
    # Async / Sync helpers #
    ########################

    async def _enque_source(
        orchestrator: BaseOrchestrator,
        request: (
            BatchConvertSourcesRequest
            | ConvertSourcesRequest
            | GenericChunkDocumentsRequest
        ),
        tenant_id: str | None = None,
    ) -> Task:
        sources: list[TaskSource] = []
        for s in request.sources:
            if isinstance(s, FileSourceRequest):
                sources.append(FileSource.model_validate(s))
            elif isinstance(s, HttpSource):
                sources.append(HttpSource.model_validate(s))
            elif isinstance(s, S3SourceRequest):
                sources.append(S3Coordinates.model_validate(s))

        convert_options: ConvertDocumentsRequestOptions
        chunking_options: BaseChunkerOptions | None = None
        chunking_export_options = ChunkingExportOptions()
        task_type: TaskType
        if isinstance(request, BatchConvertSourcesRequest | ConvertSourcesRequest):
            task_type = TaskType.CONVERT
            convert_options = request.options
        elif isinstance(request, GenericChunkDocumentsRequest):
            task_type = TaskType.CHUNK
            convert_options = request.convert_options
            chunking_options = request.chunking_options
            chunking_export_options.include_converted_doc = (
                request.include_converted_doc
            )
        else:
            raise RuntimeError("Uknown request type.")

        # Prepare metadata with tenant_id BEFORE enqueueing
        # This is critical because ray orchestrator reads tenant_id during enqueue()
        task_metadata: dict[str, str] = {}
        if tenant_id:
            task_metadata["tenant_id"] = tenant_id
            _log.info(
                f"[TENANT_ID] Preparing to enqueue with tenant_id='{tenant_id}' in metadata"
            )
        else:
            _log.warning("[TENANT_ID] No tenant_id provided, will use default")

        task = await orchestrator.enqueue(
            task_type=task_type,
            sources=sources,
            convert_options=convert_options,
            chunking_options=chunking_options,
            chunking_export_options=chunking_export_options,
            target=request.target,
            callbacks=request.callbacks,
            metadata=task_metadata,
        )

        _log.info(
            f"[TENANT_ID] Task {task.task_id} created with tenant_id='{tenant_id or 'default'}'"
        )

        return task

    async def _enque_file(
        orchestrator: BaseOrchestrator,
        files: list[UploadFile],
        task_type: TaskType,
        convert_options: ConvertDocumentsRequestOptions,
        chunking_options: BaseChunkerOptions | None,
        chunking_export_options: ChunkingExportOptions | None,
        target: TargetRequest,
        callbacks: list[CallbackSpec] | None = None,
        tenant_id: str | None = None,
    ) -> Task:
        _log.info(
            f"[TENANT_ID] _enque_file called with tenant_id='{tenant_id}', "
            f"processing {len(files)} files"
        )

        # Load the uploaded files to Docling DocumentStream
        file_sources: list[TaskSource] = []
        for i, file in enumerate(files):
            file_bytes = file.file.read()
            buf = BytesIO(file_bytes)
            suffix = "" if len(file_sources) == 1 else f"_{i}"
            name = file.filename if file.filename else f"file{suffix}.pdf"

            # Log file details for debugging transmission issues
            file_hash = hashlib.md5(file_bytes, usedforsecurity=False).hexdigest()[:12]
            _log.info(
                f"File {i}: name={name}, size={len(file_bytes)} bytes, "
                f"md5={file_hash}, content_type={file.content_type}"
            )

            file_sources.append(DocumentStream(name=name, stream=buf))

        # Prepare metadata with tenant_id BEFORE enqueueing
        metadata = {}
        if tenant_id:
            metadata["tenant_id"] = tenant_id

        task = await orchestrator.enqueue(
            task_type=task_type,
            sources=file_sources,
            convert_options=convert_options,
            chunking_options=chunking_options,
            chunking_export_options=chunking_export_options,
            target=target,
            callbacks=callbacks or [],
            metadata=metadata,
        )

        _log.info(
            f"[TENANT_ID] File task {task.task_id} created with tenant_id='{tenant_id or 'default'}'"
        )

        return task

    def _get_tenant_id_from_header(tenant_id_header: str | None) -> str:
        """Extract tenant_id from header or return default."""
        tenant_id = tenant_id_header or "default"
        _log.info(
            f"[TENANT_ID] Extracted tenant_id from header: '{tenant_id}' "
            f"(header_value: '{tenant_id_header}')"
        )
        return tenant_id

    def _task_tenant_id(task: Task) -> str:
        """Return the tenant that owns a task, defaulting to 'default'."""
        return (task.metadata or {}).get("tenant_id") or "default"

    def _assert_task_tenant(task: Task, tenant_id: str) -> None:
        """Ensure the caller's tenant owns the task.

        Raises TaskNotFoundError (surfaced as 404) on mismatch rather than 403
        so a caller cannot probe whether a task UUID exists for another tenant.

        When tenants are not in use, every task is owned by 'default' and every
        caller resolves to 'default', so this check is transparent.
        """
        owner_tenant_id = _task_tenant_id(task)
        if owner_tenant_id != tenant_id:
            _log.warning(
                f"[TENANT_ID] Tenant mismatch for task {task.task_id}: "
                f"caller='{tenant_id}' owner='{owner_tenant_id}' - denying access"
            )
            raise TaskNotFoundError()

    async def _wait_task_complete(orchestrator: BaseOrchestrator, task_id: str) -> bool:
        start_time = time.monotonic()
        while True:
            task = await orchestrator.task_status(task_id=task_id)
            if task.is_completed():
                return True
            await asyncio.sleep(docling_serve_settings.sync_poll_interval)
            elapsed_time = time.monotonic() - start_time
            if elapsed_time > docling_serve_settings.max_sync_wait:
                return False

    def _prepare_convert_request(
        request: ConvertSourcesRequest,
    ) -> ConvertSourcesRequest:
        normalized_request = normalize_request(request, service_policy)
        validate_convert_request(normalized_request, service_policy)
        return normalized_request

    def _prepare_batch_convert_request(
        request: BatchConvertSourcesRequest,
    ) -> BatchConvertSourcesRequest:
        normalized_request = normalize_request(request, service_policy)
        validate_batch_convert_request(normalized_request, service_policy)
        return normalized_request

    def _prepare_chunk_request(
        request: GenericChunkDocumentsRequest,
    ) -> GenericChunkDocumentsRequest:
        normalized_request = request.model_copy(
            update={
                "convert_options": normalize_convert_options(
                    request.convert_options, service_policy
                )
            },
            deep=True,
        )
        validate_chunk_request(normalized_request, service_policy)
        return normalized_request

    def _prepare_convert_options(
        options: ConvertDocumentsRequestOptions,
    ) -> ConvertDocumentsRequestOptions:
        normalized_options = normalize_convert_options(options, service_policy)
        validate_convert_options(normalized_options, service_policy)
        return normalized_options

    def _validate_multipart_target_type(target_type: TargetName) -> None:
        validate_target_kind(target_type.value, service_policy)

    def _check_file_upload(files: list[UploadFile], target_type: TargetName) -> None:
        if len(files) > service_policy.max_sources_per_request:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    f"Too many files: {len(files)} exceeds the "
                    f"maximum of {service_policy.max_sources_per_request}."
                ),
            )
        if (
            target_type == TargetName.PRESIGNED_URL
            and not service_policy.artifact_storage_enabled
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "Presigned URL target requires artifact storage to be configured "
                    "and enabled on the server."
                ),
            )

    def _resolve_file_target(target_type: TargetName) -> TargetRequest:
        if target_type == TargetName.PRESIGNED_URL:
            return PresignedUrlTarget()
        if target_type == TargetName.ZIP:
            return ZipTarget()
        return InBodyTarget()

    ##########################################
    # Downgrade openapi 3.1 to 3.0.x helpers #
    ##########################################

    def ensure_array_items(schema):
        """Ensure that array items are defined."""
        if "type" in schema and schema["type"] == "array":
            if "items" not in schema or schema["items"] is None:
                schema["items"] = {"type": "string"}
            elif isinstance(schema["items"], dict):
                if "type" not in schema["items"]:
                    schema["items"]["type"] = "string"

    def handle_discriminators(schema):
        """Ensure that discriminator properties are included in required."""
        if "discriminator" in schema and "propertyName" in schema["discriminator"]:
            prop = schema["discriminator"]["propertyName"]
            if "properties" in schema and prop in schema["properties"]:
                if "required" not in schema:
                    schema["required"] = []
                if prop not in schema["required"]:
                    schema["required"].append(prop)

    def handle_properties(schema):
        """Ensure that property 'kind' is included in required."""
        if "properties" in schema and "kind" in schema["properties"]:
            if "required" not in schema:
                schema["required"] = []
            if "kind" not in schema["required"]:
                schema["required"].append("kind")

    # Downgrade openapi 3.1 to 3.0.x
    def downgrade_openapi31_to_30(spec):
        def strip_unsupported(obj):
            if isinstance(obj, dict):
                obj = {
                    k: strip_unsupported(v)
                    for k, v in obj.items()
                    if k not in ("const", "examples", "prefixItems")
                }

                handle_discriminators(obj)
                ensure_array_items(obj)

                # Check for oneOf and anyOf to handle nested schemas
                for key in ["oneOf", "anyOf"]:
                    if key in obj:
                        for sub in obj[key]:
                            handle_discriminators(sub)
                            ensure_array_items(sub)

                return obj
            elif isinstance(obj, list):
                return [strip_unsupported(i) for i in obj]
            return obj

        if "components" in spec and "schemas" in spec["components"]:
            for schema_name, schema in spec["components"]["schemas"].items():
                handle_properties(schema)

        return strip_unsupported(copy.deepcopy(spec))

    #############################
    # API Endpoints definitions #
    #############################

    @app.get("/openapi-3.0.json")
    def openapi_30():
        spec = app.openapi()
        downgraded = downgrade_openapi31_to_30(spec)
        downgraded["openapi"] = "3.0.3"
        return JSONResponse(downgraded)

    # Favicon
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        logo_url = "https://raw.githubusercontent.com/docling-project/docling/refs/heads/main/docs/assets/logo.svg"
        if offline_docs_assets:
            logo_url = "/static/logo.svg"
        response = RedirectResponse(url=logo_url)
        return response

    @app.get("/health", tags=["health"])
    def health() -> HealthCheckResponse:
        _log.info("Health check requested")
        _log.debug("Processing health check")
        return HealthCheckResponse()

    @app.get("/ready", tags=["health"])
    async def readiness() -> ReadinessResponse:
        # Gate on model loading (LocalOrchestrator only; instant for RQ).
        if not _models_ready.is_set():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Models not yet loaded",
            )

        if _queue_processor_failed.is_set():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Background queue processor is not running.",
            )

        orchestrator = get_async_orchestrator()
        try:
            await orchestrator.check_connection()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=build_public_http_detail(
                    exc=exc,
                    debug_enabled=docling_serve_settings.debug_error_details,
                    fallback_message="Readiness check failed",
                ),
            ) from exc

        return ReadinessResponse()

    @app.get("/readyz", tags=["health"], include_in_schema=False)
    async def readyz() -> ReadinessResponse:
        return await readiness()

    @app.get("/livez", tags=["health"], include_in_schema=False)
    async def livez() -> HealthCheckResponse:
        # Fail liveness if the orchestrator loop has died so the platform
        # restarts the pod (which re-subscribes the pub/sub listener).
        if _queue_processor_failed.is_set():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Background queue processor is not running.",
            )
        return HealthCheckResponse()

    # API readiness compatibility for OpenShift AI Workbench
    @app.get("/api", include_in_schema=False)
    def api_check() -> HealthCheckResponse:
        return HealthCheckResponse()

    # Docling versions
    @app.get("/version", tags=["health"])
    def version_info() -> dict:
        if not docling_serve_settings.show_version_info:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden. The server is configured for not showing version details.",
            )
        return DOCLING_VERSIONS

    # Prometheus metrics endpoint
    @app.get("/metrics", tags=["health"], include_in_schema=False)
    def metrics():
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse(
            content=get_metrics_endpoint_content(),
            media_type="text/plain; version=0.0.4",
        )

    # Convert a document from URL(s)
    @app.post(
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
        conversion_request: ConvertSourcesRequestModel,
        x_tenant_id: Annotated[
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        prepared_request = _prepare_convert_request(conversion_request)
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        _log.info(f"[TENANT_ID] process_url endpoint received tenant_id='{tenant_id}'")
        task = await _enque_source(
            orchestrator=orchestrator, request=prepared_request, tenant_id=tenant_id
        )
        completed = await _wait_task_complete(
            orchestrator=orchestrator, task_id=task.task_id
        )

        if not completed:
            # TODO: abort task!
            raise HTTPException(
                status_code=504,
                detail=f"Conversion is taking too long. The maximum wait time is configure as DOCLING_SERVE_MAX_SYNC_WAIT={docling_serve_settings.max_sync_wait}.",
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
    @app.post(
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
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        _check_file_upload(files, target_type)
        options = _prepare_convert_options(options)
        _validate_multipart_target_type(target_type)
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        _log.info(f"[TENANT_ID] process_file endpoint received tenant_id='{tenant_id}'")
        target = _resolve_file_target(target_type)
        task = await _enque_file(
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
        completed = await _wait_task_complete(
            orchestrator=orchestrator, task_id=task.task_id
        )

        if not completed:
            # TODO: abort task!
            raise HTTPException(
                status_code=504,
                detail=f"Conversion is taking too long. The maximum wait time is configure as DOCLING_SERVE_MAX_SYNC_WAIT={docling_serve_settings.max_sync_wait}.",
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
    @app.post(
        "/v1/convert/source/async",
        tags=["convert"],
        response_model=TaskStatusResponse,
    )
    async def process_url_async(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        conversion_request: ConvertSourcesRequestModel,
        x_tenant_id: Annotated[
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        prepared_request = _prepare_convert_request(conversion_request)
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        _log.info(
            f"[TENANT_ID] process_url_async endpoint received tenant_id='{tenant_id}'"
        )
        task = await _enque_source(
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

    @app.post(
        "/v1/convert/source/batch",
        tags=["convert"],
        response_model=TaskStatusResponse,
    )
    async def process_source_batch(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        conversion_request: BatchConvertSourcesRequest,
        x_tenant_id: Annotated[
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        conversion_request = _prepare_batch_convert_request(conversion_request)
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        _log.info(
            f"[TENANT_ID] process_source_batch endpoint received tenant_id='{tenant_id}'"
        )
        task = await _enque_source(
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

    # Knowledge-graph extraction (NER replacement) over already-converted text.
    # Conversion is docling's OOTB job; this only runs docling-graph on the text
    # the caller already produced. Body: {text, template?, profile?}. Returns
    # {nodes, edges, labels, edgeLabels, nodeCount, edgeCount, ...}; an empty graph
    # plus a `note` when graph extraction is unconfigured/unavailable, so callers
    # (pytology) degrade uniformly instead of erroring.
    @app.post(
        "/v1/graph/extract",
        tags=["graph"],
        response_model=GraphExtractResponse,
        summary="Extract a knowledge graph (entities + relations) from converted text",
    )
    def extract_graph(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        body: GraphExtractRequest,
        x_tenant_id: Annotated[
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
    ) -> GraphExtractResponse:
        """Run docling-graph entity/relation extraction over already-converted text
        (the NER replacement) — conversion itself is docling's OOTB job.

        Body: ``{text, template?, profile?}`` (``profile`` selects a built-in
        template, e.g. ``schematic``/``access``; ``template`` is a dotted import
        path). Returns ``{nodes, edges, labels, edgeLabels, nodeCount, edgeCount}``,
        or an empty graph + a ``note`` when extraction is unconfigured so callers
        degrade uniformly.
        """
        template = body.template or resolve_profile_template(body.profile)
        # Forward the tenant to the proxy as a spend tag (the proxy key is
        # service-scoped, so this is how graph-extraction spend is attributed).
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        identity_headers = {"x-tenant-id": tenant_id} if tenant_id else None
        try:
            payload = graph_payload_from_text(
                body.text, template=template, identity_headers=identity_headers
            )
        except GraphExtractionUnavailable as err:
            _log.info("Graph extraction unavailable: %s", err)
            return GraphExtractResponse(note=str(err))
        return GraphExtractResponse(**payload)

    # Microsoft Access (.mdb/.accdb) — docling has no Access backend, so this gap is
    # filled by converting the database to docling-native markdown tables (access-parser),
    # which chunking + graph extraction then consume out of the box.
    @app.post(
        "/v1/extract/access",
        tags=["extract"],
        summary="Convert a Microsoft Access database to docling-native markdown tables",
    )
    async def extract_access(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        files: list[UploadFile],
    ):
        """Convert an uploaded Access database (.mdb/.accdb) into docling-native
        GitHub-flavored markdown — one section + table per Access table — using the
        pure-Python access-parser (no ODBC/mdbtools).

        Returns ``{filename, markdown, tables: [{name, columns, rows}], schema}``;
        ``422`` if the upload is not an Access file.
        """
        import tempfile
        from pathlib import Path as _Path

        from docling_serve.access import (
            AccessToolsUnavailableError,
            access_to_markdown,
            is_access_file,
        )
        from docling_serve.access.extract import dump_schema

        if not files:
            raise HTTPException(status_code=422, detail="No file uploaded.")
        upload = files[0]
        name = upload.filename or "database.mdb"
        if not is_access_file(name):
            raise HTTPException(
                status_code=422, detail="extract/access expects a .mdb or .accdb file."
            )
        data = await upload.read()
        tmp_path: _Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=_Path(name).suffix.lower(), delete=False
            ) as handle:
                handle.write(data)
                tmp_path = _Path(handle.name)
            markdown, tables = access_to_markdown(tmp_path)
            schema = dump_schema(tmp_path)
        except AccessToolsUnavailableError as err:
            raise HTTPException(status_code=503, detail=str(err)) from err
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
        return {"filename": name, "markdown": markdown, "tables": tables, "schema": schema}

    # XFA / AF form — Adobe LiveCycle "dynamic" PDFs (AF IMT, DoD e-Publishing) hide
    # the real form in XML packets docling's PDF/OCR pipeline can't see, so this reads
    # the XFA packets directly (pikepdf) into the captify.form.v1 payload + a markdown
    # rendering the normal chunk/index/graph path can consume.
    @app.post(
        "/v1/extract/form",
        tags=["extract"],
        summary="Extract an XFA / Air Force dynamic PDF form (captify.form.v1)",
    )
    async def extract_form_route(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        files: list[UploadFile],
        prefix: Annotated[str, Form()] = "",
        bucket: Annotated[str, Form()] = "",
    ):
        """Extract an uploaded XFA / Air Force dynamic PDF form (Adobe LiveCycle, e.g.
        AF IMT / AFMC e-Publishing forms) into the ``captify.form.v1`` payload: one
        section unit per form section, the flat ``fields`` catalog (each with its
        xfaPath, caption, bound value, options and absolute-mm bbox), plus a markdown
        rendering so the form chunks/indexes/graphs like any other document. ``422``
        when the PDF carries no XFA form.

        When ``prefix`` (+ optional ``bucket``) is given, the bundle —
        ``extraction.json`` (``domain == "form"``) + ``form.json`` + ``form.md`` +
        ``xfa-fields.json`` + the raw ``xfa-template.xml`` / ``xfa-datasets.xml``
        sidecars — is published to ``s3://{bucket}/{prefix}/`` for the ingestion
        projections + the form registrar; otherwise the payload is returned inline.
        """
        import json as _json
        import shutil as _shutil
        import tempfile
        from pathlib import Path as _Path

        from docling_serve.form import (
            XfaToolsUnavailableError,
            extract_xfa_form,
            is_xfa_pdf,
            read_xfa_packets,
        )
        from docling_serve.schematic.extract import publish_dir_to_s3

        if not files:
            raise HTTPException(status_code=422, detail="No file uploaded.")
        upload = files[0]
        name = upload.filename or "form.pdf"
        if not name.lower().endswith(".pdf"):
            raise HTTPException(status_code=422, detail="extract/form expects a .pdf file.")
        data = await upload.read()
        work = _Path(tempfile.mkdtemp(prefix="xfa-form-"))
        try:
            src = work / name
            src.write_bytes(data)
            try:
                if not is_xfa_pdf(src):
                    raise HTTPException(
                        status_code=422,
                        detail="This PDF carries no XFA form (not an AF/LiveCycle dynamic form).",
                    )
                payload = extract_xfa_form(src, source_key=name)
                packets = read_xfa_packets(src)
            except XfaToolsUnavailableError as err:
                raise HTTPException(status_code=503, detail=str(err)) from err

            target_bucket = (
                bucket or docling_serve_settings.artifact_storage_bucket or ""
            ).strip()
            published: list[str] = []
            if prefix and target_bucket:
                bundle = work / "bundle"
                bundle.mkdir(parents=True, exist_ok=True)
                (bundle / "form.json").write_text(
                    _json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                (bundle / "form.md").write_text(payload.get("markdown") or "", encoding="utf-8")
                (bundle / "xfa-fields.json").write_text(
                    _json.dumps(
                        {
                            "source": name,
                            "fieldCount": payload.get("fieldCount"),
                            "labelCount": payload.get("labelCount"),
                            "boundValueCount": payload.get("boundValueCount"),
                            "fields": payload.get("fields") or [],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                for packet_name in ("template", "datasets"):
                    raw = packets.get(packet_name)
                    if raw:
                        (bundle / f"xfa-{packet_name}.xml").write_bytes(raw)
                (bundle / "extraction.json").write_text(
                    _json.dumps(
                        {
                            "domain": "form",
                            "form": {
                                "format": "xfa",
                                "payload": "form.json",
                                "markdown": "form.md",
                                "fieldCatalog": "xfa-fields.json",
                                "template": "xfa-template.xml",
                                "datasets": "xfa-datasets.xml" if payload.get("hasDatasets") else None,
                            },
                            "fieldCount": payload.get("fieldCount"),
                            "sections": payload.get("sections"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                published = publish_dir_to_s3(bundle, bucket=target_bucket, prefix=prefix)
            return {
                **payload,
                "s3Keys": published,
                "bucket": target_bucket if published else None,
                "prefix": prefix if published else None,
            }
        finally:
            _shutil.rmtree(work, ignore_errors=True)

    # Technical Order (IPB/RPSTL) — the master parts list is a layout-aligned table
    # docling's reading-order export doesn't preserve, so this runs the deterministic
    # poppler-layout + MPL parser as an added pass and returns the captify.bom.v1 payload.
    @app.post(
        "/v1/extract/technical-order",
        tags=["extract"],
        summary="Extract an IPB/RPSTL technical order's parts list (captify.bom.v1)",
    )
    async def extract_technical_order_route(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        files: list[UploadFile],
        prefix: Annotated[str, Form()] = "",
        bucket: Annotated[str, Form()] = "",
    ):
        """Parse an uploaded IPB/RPSTL technical-order PDF into the ``captify.bom.v1``
        parts payload: every part with figure & index, part number, CAGE/FSCM, NSN,
        SMR code, description, quantity and indenture, plus the figure records
        (number, title, page, rendered drawing, callout↔part hotspots). Born-digital
        TOs use the deterministic poppler+MPL/RPSTL parser; scanned/dirty-OCR TOs use
        a vision (Sonnet 4.5) parts-table + page-classifier pass.

        When ``prefix`` (+ optional ``bucket``) is given, the bundle —
        ``extraction.json`` (``domain == "technical-order"``) + ``bom.json`` +
        ``media/`` figures — is published to ``s3://{bucket}/{prefix}/`` for the
        ingestion projections; otherwise the payload is returned inline.
        """
        import json as _json
        import shutil as _shutil
        import tempfile
        from pathlib import Path as _Path

        from docling_serve.schematic.extract import publish_dir_to_s3
        from docling_serve.technical_order.extract import extract_technical_order

        if not files:
            raise HTTPException(status_code=422, detail="No file uploaded.")
        upload = files[0]
        name = upload.filename or "to.pdf"
        if not name.lower().endswith(".pdf"):
            raise HTTPException(
                status_code=422, detail="extract/technical-order expects a .pdf file."
            )
        data = await upload.read()
        work = _Path(tempfile.mkdtemp(prefix="technical-order-"))
        try:
            src = work / name
            src.write_bytes(data)
            # When publishing, figures render into the bundle's media/ dir so the
            # callout<->part hotspots + figure images publish with the BOM.
            target_bucket = (
                bucket or docling_serve_settings.artifact_storage_bucket or ""
            ).strip()
            will_publish = bool(prefix and target_bucket)
            bundle = work / "bundle"
            media_dir = (bundle / "media") if will_publish else None
            # Sonnet-4.5 vision recall booster for figure callouts OCR misses
            # (LiteLLM/Bedrock). Only when enabled + the proxy is configured.
            vision_cfg = None
            if (
                docling_serve_settings.figure_hotspot_vision
                and docling_serve_settings.litellm_base_url
                and docling_serve_settings.litellm_api_key
            ):
                vision_cfg = {
                    "base_url": docling_serve_settings.litellm_base_url,
                    "api_key": docling_serve_settings.litellm_api_key,
                    "model": docling_serve_settings.bedrock_vision_model,
                    "min_recall": docling_serve_settings.figure_hotspot_vision_min_recall,
                    "max_calls": docling_serve_settings.figure_hotspot_vision_max_calls,
                    "parts_enabled": docling_serve_settings.vision_parts,
                    "parts_max_pages": docling_serve_settings.vision_parts_max_pages,
                }
            payload = extract_technical_order(
                src, source_key=name, media_dir=media_dir, vision=vision_cfg
            )

            # Lay down the same S3 bundle the consumers poll: extraction.json
            # (domain == "technical-order") + bom.json (captify.bom.v1) + media/
            # figure sheets. Only for an actual parts list — a non-TO PDF parses
            # to zero entries and must not create empty BOM scaffolding.
            published: list[str] = []
            if will_publish and int(payload.get("entryCount") or 0) > 0:
                bundle.mkdir(parents=True, exist_ok=True)
                (bundle / "bom.json").write_text(
                    _json.dumps(payload.get("bom") or {}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (bundle / "extraction.json").write_text(
                    _json.dumps(
                        {
                            "domain": "technical-order",
                            "technicalOrder": {"bom": "bom.json"},
                            "documentNumber": payload.get("documentNumber"),
                            "documentType": payload.get("documentType"),
                            "entryCount": payload.get("entryCount"),
                            "figureCount": payload.get("figureCount"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                published = publish_dir_to_s3(bundle, bucket=target_bucket, prefix=prefix)
            return {
                **payload,
                "s3Keys": published,
                "bucket": target_bucket if published else None,
                "prefix": prefix if published else None,
            }
        finally:
            _shutil.rmtree(work, ignore_errors=True)

    # Schematic (engineering drawing) extraction — the wiring GEOMETRY docling can't
    # recover (component symbols + the nets connecting their pins) is produced here as
    # a captify.schematic.v1 graph + derived artifacts (SVG, KiCad, netlist, EDML, XML).
    @app.post(
        "/v1/extract/schematic",
        tags=["extract"],
        summary="Extract an engineering drawing into a captify.schematic.v1 graph",
    )
    async def extract_schematic_route(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        files: list[UploadFile],
        profile: Annotated[str, Form()] = "schematic",
        prefix: Annotated[str, Form()] = "",
        bucket: Annotated[str, Form()] = "",
        x_tenant_id: Annotated[
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        """Derive the wiring GEOMETRY docling can't recover from an engineering
        drawing — every component symbol (refDes, type, value) and the nets
        connecting their pins — as a ``captify.schematic.v1`` graph plus the derived
        CAD artifacts (SVG, KiCad, netlist, EDML, KBL, SPICE). Vision (Bedrock) +
        geometry; ``profile="schematic"`` forces the drawing extractor.

        When ``prefix`` (+ optional ``bucket``) is given, the bundle —
        ``extraction.json`` (``domain == "schematic"``) + ``schematic/`` — is
        published to ``s3://{bucket}/{prefix}/`` for the schematic digital-twin
        check/revise/simulate endpoints; otherwise returned inline.
        """
        import shutil as _shutil
        import tempfile
        from pathlib import Path as _Path

        from docling_serve.schematic.extract import extract_schematic, publish_dir_to_s3

        if not files:
            raise HTTPException(status_code=422, detail="No file uploaded.")
        upload = files[0]
        name = upload.filename or "schematic.pdf"
        data = await upload.read()
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        work = _Path(tempfile.mkdtemp(prefix="schematic-"))
        try:
            src = work / name
            src.write_bytes(data)
            result = extract_schematic(
                src, work / "bundle", profile=profile, tenant_id=tenant_id, source_key=name
            )
            published: list[str] = []
            target_bucket = (
                bucket or docling_serve_settings.artifact_storage_bucket or ""
            ).strip()
            if prefix and target_bucket:
                published = publish_dir_to_s3(
                    work / "bundle", bucket=target_bucket, prefix=prefix
                )
            return {
                "domain": result["domain"],
                "graph": result["graph"],
                "artifacts": result["artifacts"],
                "s3Keys": published,
                "bucket": target_bucket if published else None,
                "prefix": prefix if published else None,
                "notes": result["notes"],
            }
        finally:
            _shutil.rmtree(work, ignore_errors=True)

    def _schematic_bucket(body: dict) -> str:
        return (
            str(body.get("bucket") or "") or docling_serve_settings.artifact_storage_bucket or ""
        ).strip()

    # CAD-style delivery check for a published schematic bundle (graph integrity,
    # KiCad open/ERC, netlist, KBL XSD, XML, ngspice). Body: {prefix, bucket?}.
    @app.post(
        "/v1/schematic/check",
        tags=["schematic"],
        summary="Run the CAD delivery checks on a published schematic bundle",
    )
    async def schematic_check(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        body: dict,
    ):
        """Run the CAD-style delivery checks (graph integrity, KiCad open/ERC,
        netlist, KBL XSD, XML, ngspice elaboration) on a published schematic bundle.

        Body: ``{prefix, bucket?}``. Returns ``{checks: [{name, status, detail}],
        passed}``; ``404`` when the bundle is missing.
        """
        from docling_serve.schematic.schematic_revision import check_schematic_bundle

        prefix = str(body.get("prefix") or "").strip()
        bucket = _schematic_bucket(body)
        if not (prefix and bucket):
            raise HTTPException(
                status_code=422,
                detail="prefix and a bucket (or configured artifact storage) are required.",
            )
        try:
            checks = check_schematic_bundle(bucket, prefix)
        except (FileNotFoundError, ValueError) as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        return {
            "checks": [c.as_dict() for c in checks],
            "passed": all(c.status != "fail" for c in checks),
        }

    # Apply browser edits to a schematic bundle and regenerate every derived artifact,
    # republish, and return the post-edit delivery check. Body: {prefix, bucket?, edits}.
    @app.post(
        "/v1/schematic/revise",
        tags=["schematic"],
        summary="Apply edits to a schematic bundle and regenerate every artifact",
    )
    async def schematic_revise(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        body: dict,
    ):
        """Apply component/net edits to a published schematic bundle, regenerate
        every derived artifact (KiCad, netlist, KBL, SPICE, XML, EDML), republish,
        and re-run the delivery checks.

        Body: ``{prefix, bucket?, edits: {components, nets}}`` (components/nets are
        addressed by graph id; ``delete: true`` drops a false detection). Returns
        ``{checks, passed, applied, notes}``; ``404`` when the bundle is missing.
        """
        from docling_serve.schematic.schematic_revision import revise_schematic_bundle

        prefix = str(body.get("prefix") or "").strip()
        bucket = _schematic_bucket(body)
        edits = body.get("edits")
        if not (prefix and bucket and isinstance(edits, dict)):
            raise HTTPException(
                status_code=422, detail="prefix, a bucket, and an edits object are required."
            )
        try:
            outcome = revise_schematic_bundle(bucket, prefix, edits)
        except (FileNotFoundError, ValueError) as err:
            raise HTTPException(status_code=404, detail=str(err)) from err
        return {
            "checks": [c.as_dict() for c in outcome.checks],
            "passed": all(c.status != "fail" for c in outcome.checks),
            "applied": outcome.applied,
            "notes": outcome.notes,
        }

    # DC operating-point simulation of a published schematic (real ngspice solve).
    # Body: {prefix, bucket?, sources?:[{net, volts}]}.
    @app.post(
        "/v1/schematic/simulate",
        tags=["schematic"],
        summary="Run a real ngspice DC operating-point simulation on a schematic",
    )
    async def schematic_simulate(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        body: dict,
    ):
        """Run a REAL ngspice DC operating-point solve (in-process libngspice via
        PySpice) on a published schematic: energize specific nets or use the
        auto-detected supplies.

        Body: ``{prefix, bucket?, sources?: [{net, volts}]}``. Returns the circuit
        classification, what was energized, and the resulting node voltages.
        """
        import dataclasses
        import json as _json

        import boto3

        from docling_serve.schematic.spice_simulation import simulate_graph

        prefix = str(body.get("prefix") or "").strip().strip("/")
        bucket = _schematic_bucket(body)
        if not (prefix and bucket):
            raise HTTPException(status_code=422, detail="prefix and a bucket are required.")
        client = boto3.client("s3")
        try:
            graph = _json.loads(
                client.get_object(
                    Bucket=bucket, Key=f"{prefix}/schematic/schematic-graph.json"
                )["Body"].read()
            )
        except Exception as err:
            raise HTTPException(
                status_code=404, detail=f"schematic graph not found: {err}"
            ) from err
        sources = body.get("sources") if isinstance(body.get("sources"), list) else []
        source = graph.get("source") or {}
        result = simulate_graph(
            graph,
            source_name=str(source.get("originalFileName") or "schematic.pdf"),
            sources=sources,
        )
        return {
            "ok": result.ok,
            "classification": dataclasses.asdict(result.classification),
            "supplies": result.supplies,
            "grounds": result.grounds,
            "nodeVoltages": result.nodeVoltages,
            "sourceCurrents": result.sourceCurrents,
            "warnings": result.warnings,
            "engine": result.engine,
        }

    # Convert a document from file(s) using the async api
    @app.post(
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
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        _check_file_upload(files, target_type)
        options = _prepare_convert_options(options)
        _validate_multipart_target_type(target_type)
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        _log.info(
            f"[TENANT_ID] process_file_async endpoint received tenant_id='{tenant_id}'"
        )
        target = _resolve_file_target(target_type)
        task = await _enque_file(
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

        @app.post(
            f"/v1/chunk/{path_name}/source/async",
            name=f"Chunk sources with {display_name} as async task",
            tags=["chunk"],
            response_model=TaskStatusResponse,
        )
        async def chunk_source_async(
            background_tasks: BackgroundTasks,
            auth: Annotated[AuthenticationResult, Depends(require_auth)],
            orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
            request: req_cls,
            x_tenant_id: Annotated[
                str | None,
                Header(alias=docling_serve_settings.eng_ray_tenant_id_header),
            ] = None,
        ):
            request = _prepare_chunk_request(request)
            tenant_id = _get_tenant_id_from_header(x_tenant_id)
            _log.info(
                f"[TENANT_ID] chunk_source_async ({path_name}) endpoint received tenant_id='{tenant_id}'"
            )
            task = await _enque_source(
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

        @app.post(
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
            chunking_options: Annotated[
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
                Header(alias=docling_serve_settings.eng_ray_tenant_id_header),
            ] = None,
        ):
            if target_type == TargetName.PRESIGNED_URL:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="presigned_url target is not supported for chunk endpoints.",
                )
            convert_options = _prepare_convert_options(convert_options)
            _validate_multipart_target_type(target_type)
            tenant_id = _get_tenant_id_from_header(x_tenant_id)
            _log.info(
                f"[TENANT_ID] chunk_file_async ({path_name}) endpoint received tenant_id='{tenant_id}'"
            )
            target = InBodyTarget() if target_type == TargetName.INBODY else ZipTarget()
            task = await _enque_file(
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

        @app.post(
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
            request: req_cls,
            x_tenant_id: Annotated[
                str | None,
                Header(alias=docling_serve_settings.eng_ray_tenant_id_header),
            ] = None,
        ):
            request = _prepare_chunk_request(request)
            tenant_id = _get_tenant_id_from_header(x_tenant_id)
            _log.info(
                f"[TENANT_ID] chunk_source ({path_name}) endpoint received tenant_id='{tenant_id}'"
            )
            task = await _enque_source(
                orchestrator=orchestrator, request=request, tenant_id=tenant_id
            )
            completed = await _wait_task_complete(
                orchestrator=orchestrator, task_id=task.task_id
            )

            if not completed:
                # TODO: abort task!
                raise HTTPException(
                    status_code=504,
                    detail=f"Conversion is taking too long. The maximum wait time is configure as DOCLING_SERVE_MAX_SYNC_WAIT={docling_serve_settings.max_sync_wait}.",
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

        @app.post(
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
            chunking_options: Annotated[
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
                Header(alias=docling_serve_settings.eng_ray_tenant_id_header),
            ] = None,
        ):
            if target_type == TargetName.PRESIGNED_URL:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="presigned_url target is not supported for chunk endpoints.",
                )
            convert_options = _prepare_convert_options(convert_options)
            _validate_multipart_target_type(target_type)
            tenant_id = _get_tenant_id_from_header(x_tenant_id)
            _log.info(
                f"[TENANT_ID] chunk_file ({path_name}) endpoint received tenant_id='{tenant_id}'"
            )
            target = InBodyTarget() if target_type == TargetName.INBODY else ZipTarget()
            task = await _enque_file(
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
            completed = await _wait_task_complete(
                orchestrator=orchestrator, task_id=task.task_id
            )

            if not completed:
                # TODO: abort task!
                raise HTTPException(
                    status_code=504,
                    detail=f"Conversion is taking too long. The maximum wait time is configure as DOCLING_SERVE_MAX_SYNC_WAIT={docling_serve_settings.max_sync_wait}.",
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

    # Task status poll
    @app.get(
        "/v1/status/poll/{task_id}",
        tags=["tasks"],
        response_model=TaskStatusResponse,
    )
    async def task_status_poll(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        task_id: str,
        x_tenant_id: Annotated[
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
        wait: Annotated[
            float,
            Query(description="Number of seconds to wait for a completed status."),
        ] = 0.0,
    ):
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        try:
            task = await orchestrator.task_status(task_id=task_id, wait=wait)
            _assert_task_tenant(task, tenant_id)
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
    @app.websocket(
        "/v1/status/ws/{task_id}",
    )
    async def task_status_ws(
        websocket: WebSocket,
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        task_id: str,
        api_key: Annotated[str, Query()] = "",
        tenant_id: Annotated[str | None, Query()] = None,
    ):
        if docling_serve_settings.api_key:
            # WebSocket clients on this endpoint authenticate via query
            # parameter. Note that query-parameter keys may be captured in
            # proxy/access logs.
            if api_key != docling_serve_settings.api_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=(
                        "Api key is required as the ?api_key=SECRET query parameter."
                    ),
                )

        tenant_id = tenant_id or "default"

        assert isinstance(orchestrator.notifier, WebsocketNotifier)
        await websocket.accept()

        try:
            task = await orchestrator.task_status(task_id=task_id)
            _assert_task_tenant(task, tenant_id)
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
    @app.get(
        "/v1/result/{task_id}",
        tags=["tasks"],
        response_model=ConvertDocumentResponse
        | PresignedUrlConvertDocumentResponse
        | PresignedUrlConvertResponse
        | ChunkDocumentResponse
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
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
    ):
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        try:
            task = await orchestrator.task_status(task_id=task_id)
            _assert_task_tenant(task, tenant_id)
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
    @app.post(
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
                    debug_enabled=docling_serve_settings.debug_error_details,
                    fallback_message="Invalid progress payload.",
                ),
            )

    #### Clear requests

    # Offload models
    @app.get(
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
    @app.get(
        "/v1/clear/results",
        tags=["clear"],
        response_model=ClearResponse,
    )
    async def clear_results(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        orchestrator: Annotated[BaseOrchestrator, Depends(get_async_orchestrator)],
        older_then: float = 3600,
    ):
        await orchestrator.clear_results(older_than=older_then)
        return ClearResponse()

    @app.get("/v1/memory/stats", tags=["management"])
    async def memory_stats():
        if not docling_serve_settings.enable_management_endpoints:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Forbidden. The server is configured for not showing internal managament details.",
            )
        process = psutil.Process(os.getpid())
        rss_mb = process.memory_info().rss / 1024 / 1024
        stats = {}

        # total memory (this is what triggers OOM)
        with open("/sys/fs/cgroup/memory.current") as f:  # noqa: ASYNC230
            stats["cgroup_total"] = int(f.read()) / 1024 / 1024

        # detailed breakdown
        with open("/sys/fs/cgroup/memory.stat") as f:  # noqa: ASYNC230
            for line in f:
                key, value = line.split()
                stats[key] = int(value) / 1024 / 1024

        return {
            "rss": rss_mb,
            "anon": stats.get("anon", 0.0),
            "file": stats.get("file", 0.0),
            "slab": stats.get("slab", 0.0),
            "cgroup_total": stats["cgroup_total"],
        }

    @app.get("/v1/memory/counts", tags=["management"])
    async def memory_counts():
        if not docling_serve_settings.enable_management_endpoints:
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

    return app
