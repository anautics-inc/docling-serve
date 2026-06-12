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
from pathlib import Path
from typing import Annotated
from uuid import uuid4

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
from pydantic import BaseModel, Field
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
    ConvertDocumentsRequest,
    FileSourceRequest,
    GenericChunkDocumentsRequest,
    HttpSourceRequest,
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
    ReadinessResponse,
    TaskStatusResponse,
    WebsocketMessage,
)
from docling.datamodel.service.sources import FileSource, HttpSource, S3Coordinates
from docling.datamodel.service.targets import (
    InBodyTarget,
    ZipTarget,
)
from docling.datamodel.service.tasks import TaskType
from docling_jobkit.datamodel.chunking import ChunkingExportOptions
from docling_jobkit.datamodel.task import Task, TaskSource
from docling_jobkit.orchestrators.base_orchestrator import (
    BaseOrchestrator,
    ProgressInvalid,
    RedisBackpressureError,
    TaskNotFoundError,
)
from docling_jobkit.orchestrators.rq.orchestrator import RQOrchestrator

from docling_serve.auth import APIKeyAuth, AuthenticationResult
from docling_serve.deep_document.options import (
    deep_extraction_mode,
    prepare_deep_convert_options,
)
from docling_serve.deep_document.s3_publisher import (
    S3_REQUIRED_MESSAGE,
    DeepBucketNotAllowed,
    deep_bucket_available,
    ensure_bucket_allowed,
)
from docling_serve.extraction.legacy_office import (
    LegacyOfficeConversionError,
    convert_legacy_office_bytes,
    is_legacy_office,
)
from docling_serve.extractors import NON_DOCLING_SUFFIXES
from docling_serve.identity import RequestIdentity, bind_identity
from docling_serve.helper_functions import (
    DOCLING_VERSIONS,
    FormDepends,
    coerce_supported_filename,
)
from docling_serve.orchestrator_factory import get_async_orchestrator
from docling_serve.otel_instrumentation import (
    get_metrics_endpoint_content,
    setup_otel_instrumentation,
)
from docling_serve.policy import (
    build_service_policy,
    normalize_convert_options,
    normalize_convert_request,
    validate_chunk_request,
    validate_convert_options,
    validate_convert_request,
)
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


# Set up custom logging as we'll be intermixes with FastAPI/Uvicorn's logging
class ColoredLogFormatter(logging.Formatter):
    COLOR_CODES = {
        logging.DEBUG: "\033[94m",  # Blue
        logging.INFO: "\033[92m",  # Green
        logging.WARNING: "\033[93m",  # Yellow
        logging.ERROR: "\033[91m",  # Red
        logging.CRITICAL: "\033[95m",  # Magenta
    }
    RESET_CODE = "\033[0m"

    def format(self, record):
        color = self.COLOR_CODES.get(record.levelno, "")
        record.levelname = f"{color}{record.levelname}{self.RESET_CODE}"
        return super().format(record)


logging.basicConfig(
    level=logging.INFO,  # Set the logging level
    format="%(levelname)s:\t%(asctime)s - %(name)s - %(message)s",
    datefmt="%H:%M:%S",
)

# Override the formatter with the custom ColoredLogFormatter
root_logger = logging.getLogger()  # Get the root logger
for handler in root_logger.handlers:  # Iterate through existing handlers
    if handler.formatter:
        handler.setFormatter(ColoredLogFormatter(handler.formatter._fmt))

_log = logging.getLogger(__name__)

# Tracks whether warm_up_caches() has completed.  Meaningful only for the
# LocalOrchestrator (which eagerly loads ML models); the RQ orchestrator's
# implementation is a no-op so this event fires instantly in RQ deployments.
_models_ready = asyncio.Event()


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

    # Start the background queue processor
    queue_task = asyncio.create_task(orchestrator.process_queue())

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
# Knowledge-graph extraction API  #
##################################


class GraphExtractRequest(BaseModel):
    """Request body for ``POST /v1/graph/extract``."""

    text: str = Field(description="Converted document text/markdown to extract a graph from")
    template: str | None = Field(
        default=None,
        description="Dotted import path to a docling-graph Pydantic template; "
        "empty uses the configured default (generic entity/relation graph).",
    )
    profile: str | None = Field(
        default=None,
        description="Extraction profile (e.g. schematic, access); selects the matching "
        "domain template when no explicit template is given.",
    )


class ImageContextResponse(BaseModel):
    """Vision descriptions for a document's significant images."""

    images: list[dict] = Field(default_factory=list)
    count: int = 0
    #: Populated when description was skipped (e.g. Bedrock unconfigured).
    note: str | None = None


class GraphExtractResponse(BaseModel):
    """Typed entity + relationship graph extracted from text."""

    nodes: list[dict] = Field(default_factory=list)
    edges: list[dict] = Field(default_factory=list)
    labels: dict[str, int] = Field(default_factory=dict)
    edgeLabels: dict[str, int] = Field(default_factory=dict)
    nodeCount: int = 0
    edgeCount: int = 0
    template: str | None = None
    model: dict | None = None
    extractedModels: int | None = None
    #: Populated when extraction was skipped (e.g. unconfigured); empty graph returned.
    note: str | None = None


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
                content={"detail": str(exc) or "Ray dispatcher is unavailable."},
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
        request: ConvertDocumentsRequest | GenericChunkDocumentsRequest,
        tenant_id: str | None = None,
        identity: RequestIdentity | None = None,
    ) -> Task:
        sources: list[TaskSource] = []
        for s in request.sources:
            if isinstance(s, FileSourceRequest):
                sources.append(FileSource.model_validate(s))
            elif isinstance(s, HttpSourceRequest):
                sources.append(HttpSource.model_validate(s))
            elif isinstance(s, S3SourceRequest):
                sources.append(S3Coordinates.model_validate(s))

        convert_options: ConvertDocumentsRequestOptions
        chunking_options: BaseChunkerOptions | None = None
        chunking_export_options = ChunkingExportOptions()
        task_type: TaskType
        if isinstance(request, ConvertDocumentsRequest):
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
        metadata = {}
        if tenant_id:
            metadata["tenant_id"] = tenant_id
            _log.info(
                f"[TENANT_ID] Preparing to enqueue with tenant_id='{tenant_id}' in metadata"
            )
        else:
            _log.warning("[TENANT_ID] No tenant_id provided, will use default")
        # Caller identity for worker-side LLM spend attribution and audit.
        if identity is not None:
            if identity.actor_id:
                metadata["actor_id"] = identity.actor_id
            if identity.request_id:
                metadata["request_id"] = identity.request_id

        task = await orchestrator.enqueue(
            task_type=task_type,
            sources=sources,
            convert_options=convert_options,
            chunking_options=chunking_options,
            chunking_export_options=chunking_export_options,
            target=request.target,
            callbacks=request.callbacks,
            metadata=metadata,
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
        identity: RequestIdentity | None = None,
        extraction: str = "default",
        deep_s3_bucket: str = "",
        deep_s3_prefix: str = "",
        profile: str = "default",
        enhancements: str = "",
    ) -> Task:
        _log.info(
            f"[TENANT_ID] _enque_file called with tenant_id='{tenant_id}', "
            f"processing {len(files)} files"
        )

        # Deep extraction reconstructs the original file with native parsers
        # (e.g. python-pptx needs real geometry) but Docling only sees an
        # in-memory stream. Persist the raw uploads to a scratch dir so the deep
        # worker can re-open the true bytes by filename at assemble time. The
        # same applies to chunk uploads owned by a non-docling extractor (e.g.
        # an Access DB read natively via mdbtools): the worker needs the real
        # bytes because docling can never convert them.
        deep_source_dir: Path | None = None
        needs_source_bytes = any(
            Path(file.filename or "").suffix.lower() in NON_DOCLING_SUFFIXES
            for file in files
        )
        if deep_extraction_mode(extraction) or needs_source_bytes:
            deep_source_dir = get_scratch() / "deep-sources" / uuid4().hex
            deep_source_dir.mkdir(parents=True, exist_ok=True)

        # Load the uploaded files to Docling DocumentStream
        file_sources: list[TaskSource] = []
        legacy_sources: dict[str, str] = {}
        for i, file in enumerate(files):
            file_bytes = file.file.read()
            suffix = "" if len(file_sources) == 1 else f"_{i}"
            name = file.filename if file.filename else f"file{suffix}.pdf"
            # Let plain-text uploads (.txt, .log, …) ride Docling's Markdown backend instead of being
            # rejected for having no dedicated parser — so every text document type ingests.
            name = coerce_supported_filename(name)

            # Legacy binary Office uploads (.doc/.xls/.ppt) have no docling
            # backend: pre-convert to the modern equivalent with headless
            # LibreOffice so the normal extractor chain runs unchanged. The
            # original filename is preserved in task metadata; a conversion
            # failure is a typed, user-readable error — not a docling failure.
            if is_legacy_office(name):
                original_name = name
                try:
                    name, file_bytes = await asyncio.to_thread(
                        convert_legacy_office_bytes, name, file_bytes
                    )
                except LegacyOfficeConversionError as err:
                    if deep_source_dir is not None:
                        shutil.rmtree(deep_source_dir, ignore_errors=True)
                    raise HTTPException(
                        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                        detail=str(err),
                    ) from err
                legacy_sources[name] = original_name
                _log.info(
                    f"Pre-converted legacy Office upload '{original_name}' "
                    f"to '{name}' via LibreOffice."
                )

            buf = BytesIO(file_bytes)

            # Log file details for debugging transmission issues
            file_hash = hashlib.md5(file_bytes, usedforsecurity=False).hexdigest()[:12]
            _log.info(
                f"File {i}: name={name}, size={len(file_bytes)} bytes, "
                f"md5={file_hash}, content_type={file.content_type}"
            )

            if deep_source_dir is not None:
                (deep_source_dir / Path(name).name).write_bytes(file_bytes)

            file_sources.append(DocumentStream(name=name, stream=buf))

        # Prepare metadata with tenant_id BEFORE enqueueing
        metadata = {}
        if tenant_id:
            metadata["tenant_id"] = tenant_id
        # Caller identity for worker-side LLM spend attribution and audit.
        if identity is not None:
            if identity.actor_id:
                metadata["actor_id"] = identity.actor_id
            if identity.request_id:
                metadata["request_id"] = identity.request_id
        # Per-request extraction mode: "default" (legacy response) or "deep"
        # (full structural extraction + S3 expanded object tree). For "deep",
        # the caller may supply the S3 destination directly.
        metadata["extraction"] = extraction
        # Per-request extraction profile (selects the domain extractor, e.g.
        # schematic/access/auto) and opt-in enhancers (e.g. image_context).
        if profile and profile != "default":
            metadata["profile"] = profile
        if enhancements:
            metadata["enhancements"] = enhancements
        if deep_s3_bucket:
            metadata["deep_s3_bucket"] = deep_s3_bucket
        if deep_s3_prefix:
            metadata["deep_s3_prefix"] = deep_s3_prefix
        if deep_source_dir is not None:
            metadata["deep_source_dir"] = str(deep_source_dir)
        # Converted name -> original upload name for LibreOffice-pre-converted
        # legacy Office files, so results can report the user's filename.
        if legacy_sources:
            metadata["legacy_office_sources"] = legacy_sources

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

        # The local orchestrator drops the enqueue() metadata argument, so the
        # worker would never see the extraction mode. Assign it onto the
        # returned task directly (the local orchestrator hands the worker this
        # same object; the RQ orchestrator already serialized metadata above).
        task.metadata = {**(task.metadata or {}), **metadata}

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

    def _caller_identity(request: Request) -> RequestIdentity | None:
        """Authenticated caller forwarded by the captify gateway, if present.

        The gateway (pytology) Cognito-validates the user and forwards
        identity headers; this service trusts them on the private interface.
        Identity rides on task metadata so worker-side model calls are
        attributed to the originating user/tenant in LiteLLM spend logs.
        """
        headers = request.headers
        tenant_id = headers.get(docling_serve_settings.eng_ray_tenant_id_header)
        actor_id = headers.get(docling_serve_settings.actor_id_header)
        request_id = headers.get(docling_serve_settings.request_id_header)
        if not (tenant_id or actor_id or request_id):
            return None
        return RequestIdentity(
            tenant_id=tenant_id, actor_id=actor_id, request_id=request_id
        )

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
        request: ConvertDocumentsRequest,
    ) -> ConvertDocumentsRequest:
        normalized_request = normalize_convert_request(request, service_policy)
        validate_convert_request(normalized_request, service_policy)
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
        return HealthCheckResponse()

    @app.get("/ready", tags=["health"])
    async def readiness() -> ReadinessResponse:
        # Gate on model loading (LocalOrchestrator only; instant for RQ).
        if not _models_ready.is_set():
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Models not yet loaded",
            )

        orchestrator = get_async_orchestrator()
        try:
            await orchestrator.check_connection()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(exc) or "Readiness check failed",
            ) from exc

        return ReadinessResponse()

    @app.get("/readyz", tags=["health"], include_in_schema=False)
    async def readyz() -> ReadinessResponse:
        return await readiness()

    @app.get("/livez", tags=["health"], include_in_schema=False)
    async def livez() -> HealthCheckResponse:
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

    # Describe a document's images with the vision model. Synchronous, stateless:
    # callers upload the original file; significant raster images (PPTX picture
    # shapes, PDF embedded images) are extracted, de-duplicated, and described so
    # the ingest path can index/graph image-borne content (charts, diagrams,
    # screenshots) that text conversion renders as placeholders. Returns an empty
    # list (not an error) when Bedrock is unconfigured.
    @app.post("/v1/images/context", tags=["images"])
    def images_context(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        files: list[UploadFile],
        max_images: Annotated[int, Form()] = 0,
    ) -> ImageContextResponse:
        import tempfile

        from docling_serve.extractors.enhancers import (
            ImageContextUnavailable,
            describe_file_images,
        )

        upload = files[0]
        suffix = Path(upload.filename or "upload.bin").suffix
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            shutil.copyfileobj(upload.file, handle)
            tmp_path = Path(handle.name)
        try:
            images = describe_file_images(
                tmp_path, max_images=max_images or None
            )
        except ImageContextUnavailable as err:
            _log.info("Image context unavailable: %s", err)
            return ImageContextResponse(images=[], note=str(err))
        finally:
            tmp_path.unlink(missing_ok=True)
        return ImageContextResponse(images=images, count=len(images))

    # Extract a knowledge graph (typed entities + relationships) from text.
    # Synchronous, stateless: callers pass already-converted markdown/text and
    # get back a node/edge payload. This is the entity-extraction service that
    # replaces generic NER — the LLM call runs through the configured LiteLLM
    # proxy via docling-graph. Returns an empty graph (not an error) when the
    # feature is unconfigured or docling-graph is not installed, so a caller can
    # treat "no graph" uniformly.
    @app.post("/v1/graph/extract", tags=["graph"])
    def extract_graph(
        auth: Annotated[AuthenticationResult, Depends(require_auth)],
        body: GraphExtractRequest,
        identity: Annotated[
            RequestIdentity | None, Depends(_caller_identity)
        ] = None,
    ) -> GraphExtractResponse:
        from docling_serve.extractors.enhancers import (
            GraphExtractionUnavailable,
            graph_payload_from_text,
            resolve_profile_template,
        )

        template = body.template or resolve_profile_template(body.profile)
        try:
            with bind_identity(identity):
                payload = graph_payload_from_text(body.text, template=template)
        except GraphExtractionUnavailable as err:
            _log.info("Graph extraction unavailable: %s", err)
            return GraphExtractResponse(
                nodes=[],
                edges=[],
                labels={},
                edgeLabels={},
                nodeCount=0,
                edgeCount=0,
                note=str(err),
            )
        return GraphExtractResponse(**payload)

    # Convert a document from URL(s)
    @app.post(
        "/v1/convert/source",
        tags=["convert"],
        response_model=ConvertDocumentResponse | PresignedUrlConvertDocumentResponse,
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
        conversion_request: ConvertDocumentsRequest,
        x_tenant_id: Annotated[
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
        identity: Annotated[
            RequestIdentity | None, Depends(_caller_identity)
        ] = None,
    ):
        conversion_request = _prepare_convert_request(conversion_request)
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        _log.info(f"[TENANT_ID] process_url endpoint received tenant_id='{tenant_id}'")
        task = await _enque_source(
            orchestrator=orchestrator,
            request=conversion_request,
            tenant_id=tenant_id,
            identity=identity,
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
        response_model=ConvertDocumentResponse | PresignedUrlConvertDocumentResponse,
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
        target_type: Annotated[TargetName, Form()] = TargetName.INBODY,
        extraction: Annotated[str, Form()] = "default",
        deep_s3_bucket: Annotated[str, Form()] = "",
        deep_s3_prefix: Annotated[str, Form()] = "",
        profile: Annotated[str, Form()] = "default",
        enhancements: Annotated[str, Form()] = "",
        x_tenant_id: Annotated[
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
        identity: Annotated[
            RequestIdentity | None, Depends(_caller_identity)
        ] = None,
    ):
        options = _prepare_convert_options(options)
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        _log.info(f"[TENANT_ID] process_file endpoint received tenant_id='{tenant_id}'")
        # "deep" extraction publishes an S3 expanded object tree. The bucket
        # comes from the request (deep_s3_bucket) or the server default; fail
        # explicitly only when neither is available.
        deep = deep_extraction_mode(extraction)
        if deep and not deep_bucket_available(deep_s3_bucket):
            raise HTTPException(status_code=503, detail=S3_REQUIRED_MESSAGE)
        if deep:
            try:
                ensure_bucket_allowed(deep_s3_bucket)
            except DeepBucketNotAllowed as err:
                raise HTTPException(status_code=400, detail=str(err)) from err
            options = prepare_deep_convert_options(options)
        target = ZipTarget() if target_type != TargetName.INBODY else InBodyTarget()
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
            identity=identity,
            extraction=extraction,
            deep_s3_bucket=deep_s3_bucket,
            deep_s3_prefix=deep_s3_prefix,
            profile=profile,
            enhancements=enhancements,
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
        conversion_request: ConvertDocumentsRequest,
        x_tenant_id: Annotated[
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
        identity: Annotated[
            RequestIdentity | None, Depends(_caller_identity)
        ] = None,
    ):
        conversion_request = _prepare_convert_request(conversion_request)
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        _log.info(
            f"[TENANT_ID] process_url_async endpoint received tenant_id='{tenant_id}'"
        )
        task = await _enque_source(
            orchestrator=orchestrator,
            request=conversion_request,
            tenant_id=tenant_id,
            identity=identity,
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
            error_message=getattr(task, "error_message", None),
        )

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
        target_type: Annotated[TargetName, Form()] = TargetName.INBODY,
        extraction: Annotated[str, Form()] = "default",
        deep_s3_bucket: Annotated[str, Form()] = "",
        deep_s3_prefix: Annotated[str, Form()] = "",
        profile: Annotated[str, Form()] = "default",
        enhancements: Annotated[str, Form()] = "",
        x_tenant_id: Annotated[
            str | None, Header(alias=docling_serve_settings.eng_ray_tenant_id_header)
        ] = None,
        identity: Annotated[
            RequestIdentity | None, Depends(_caller_identity)
        ] = None,
    ):
        options = _prepare_convert_options(options)
        tenant_id = _get_tenant_id_from_header(x_tenant_id)
        _log.info(
            f"[TENANT_ID] process_file_async endpoint received tenant_id='{tenant_id}'"
        )
        # "deep" extraction publishes an S3 expanded object tree. The bucket
        # comes from the request (deep_s3_bucket) or the server default; fail
        # explicitly only when neither is available.
        deep = deep_extraction_mode(extraction)
        if deep and not deep_bucket_available(deep_s3_bucket):
            raise HTTPException(status_code=503, detail=S3_REQUIRED_MESSAGE)
        if deep:
            try:
                ensure_bucket_allowed(deep_s3_bucket)
            except DeepBucketNotAllowed as err:
                raise HTTPException(status_code=400, detail=str(err)) from err
            options = prepare_deep_convert_options(options)
        target = ZipTarget() if target_type != TargetName.INBODY else InBodyTarget()
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
            identity=identity,
            extraction=extraction,
            deep_s3_bucket=deep_s3_bucket,
            deep_s3_prefix=deep_s3_prefix,
            profile=profile,
            enhancements=enhancements,
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
            error_message=getattr(task, "error_message", None),
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
            identity: Annotated[
                RequestIdentity | None, Depends(_caller_identity)
            ] = None,
        ):
            request = _prepare_chunk_request(request)
            tenant_id = _get_tenant_id_from_header(x_tenant_id)
            _log.info(
                f"[TENANT_ID] chunk_source_async ({path_name}) endpoint received tenant_id='{tenant_id}'"
            )
            task = await _enque_source(
                orchestrator=orchestrator,
                request=request,
                tenant_id=tenant_id,
                identity=identity,
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
                error_message=getattr(task, "error_message", None),
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
            identity: Annotated[
                RequestIdentity | None, Depends(_caller_identity)
            ] = None,
        ):
            convert_options = _prepare_convert_options(convert_options)
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
                identity=identity,
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
                error_message=getattr(task, "error_message", None),
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
            identity: Annotated[
                RequestIdentity | None, Depends(_caller_identity)
            ] = None,
        ):
            request = _prepare_chunk_request(request)
            tenant_id = _get_tenant_id_from_header(x_tenant_id)
            _log.info(
                f"[TENANT_ID] chunk_source ({path_name}) endpoint received tenant_id='{tenant_id}'"
            )
            task = await _enque_source(
                orchestrator=orchestrator,
                request=request,
                tenant_id=tenant_id,
                identity=identity,
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
            identity: Annotated[
                RequestIdentity | None, Depends(_caller_identity)
            ] = None,
        ):
            convert_options = _prepare_convert_options(convert_options)
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
                identity=identity,
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
        wait: Annotated[
            float,
            Query(description="Number of seconds to wait for a completed status."),
        ] = 0.0,
    ):
        try:
            task = await orchestrator.task_status(task_id=task_id, wait=wait)
            task_queue_position = await orchestrator.get_queue_position(task_id=task_id)
        except TaskNotFoundError:
            raise HTTPException(status_code=404, detail="Task not found.")
        return TaskStatusResponse(
            task_id=task.task_id,
            task_type=task.task_type,
            task_status=task.task_status,
            task_position=task_queue_position,
            task_meta=task.processing_meta,
            error_message=getattr(task, "error_message", None),
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
    ):
        if docling_serve_settings.api_key:
            if api_key != docling_serve_settings.api_key:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Api key is required as ?api_key=SECRET.",
                )

        assert isinstance(orchestrator.notifier, WebsocketNotifier)
        await websocket.accept()

        try:
            task = await orchestrator.task_status(task_id=task_id)
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
                error_message=getattr(task, "error_message", None),
            )
            await websocket.send_text(
                WebsocketMessage(
                    message=MessageKind.CONNECTION, task=task_response
                ).model_dump_json()
            )
            while True:
                task_queue_position = await orchestrator.get_queue_position(
                    task_id=task_id
                )
                task_response = TaskStatusResponse(
                    task_id=task.task_id,
                    task_type=task.task_type,
                    task_status=task.task_status,
                    task_position=task_queue_position,
                    task_meta=task.processing_meta,
                    error_message=getattr(task, "error_message", None),
                )
                await websocket.send_text(
                    WebsocketMessage(
                        message=MessageKind.UPDATE, task=task_response
                    ).model_dump_json()
                )
                # each client message will be interpreted as a request for update
                msg = await websocket.receive_text()
                _log.debug(f"Received message: {msg}")

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
        | ChunkDocumentResponse,
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
    ):
        try:
            task_result = await orchestrator.task_result(task_id=task_id)
            if task_result is None:
                raise HTTPException(
                    status_code=404,
                    detail="Task result not found. Please wait for a completion status.",
                )
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
                status_code=400, detail=f"Invalid progress payload: {err}"
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
