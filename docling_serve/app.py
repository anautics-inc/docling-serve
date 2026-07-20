import asyncio
import copy
import importlib.metadata
import logging
import shutil
from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    Request,
    status,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import (
    get_redoc_html,
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
)
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import create_model
from scalar_fastapi import get_scalar_api_reference

from docling.datamodel.service.requests import (
    ConvertSourcesRequest,
    TargetName,
    TargetRequest,
)
from docling_jobkit.orchestrators.base_orchestrator import (
    RedisBackpressureError,
)
from docling_jobkit.orchestrators.rq.orchestrator import RQOrchestrator

from docling_serve.api import composition as api_composition
from docling_serve.auth import APIKeyAuth, MachineAssertionAuth
from docling_serve.logging_config import setup_logging
from docling_serve.orchestrator_factory import get_async_orchestrator
from docling_serve.otel_instrumentation import (
    setup_otel_instrumentation,
)
from docling_serve.policy import (
    build_service_policy,
    resolve_default_target,
)
from docling_serve.settings import AsyncEngine, docling_serve_settings
from docling_serve.upload_staging import (
    build_upload_stager,
    check_upload_staging_capability,
    reconcile_cleanup_once,
)
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


async def _run_staging_cleanup_reconciler() -> None:
    stager = build_upload_stager()
    while True:
        try:
            await asyncio.to_thread(
                reconcile_cleanup_once,
                stager,
                max_items=docling_serve_settings.upload_staging_reconcile_batch_size,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.error(
                "Encrypted staged cleanup reconciliation failed; lifecycle "
                "backstop remains active"
            )
        await asyncio.sleep(
            docling_serve_settings.upload_staging_reconcile_interval_seconds
        )


# Context manager to initialize and clean up the lifespan of the FastAPI app
@asynccontextmanager
async def lifespan(app: FastAPI):
    if (
        docling_serve_settings.auth_mode == "api_key"
        and not docling_serve_settings.api_key
    ):
        if docling_serve_settings.allow_no_auth:
            # Explicit dev/test opt-in: every endpoint accepts unauthenticated
            # requests. Acceptable only for a loopback-bound instance — make
            # the posture impossible to miss in the logs.
            _log.warning(
                "DOCLING_SERVE_API_KEY is not set and DOCLING_SERVE_ALLOW_NO_AUTH "
                "is true: ALL endpoints accept unauthenticated requests. Never "
                "set this for a deployment reachable beyond localhost."
            )
        else:
            # Fail closed: no key configured and no explicit opt-in means every
            # request is refused rather than silently accepted.
            _log.warning(
                "DOCLING_SERVE_API_KEY is not set: ALL endpoints will refuse "
                "requests with 503 until an API key is configured, or "
                "DOCLING_SERVE_ALLOW_NO_AUTH=true is set for a deliberately "
                "unauthenticated dev/test instance."
            )
    scratch_dir = api_composition.get_scratch()

    # The factory cache is process-global, but an orchestrator's asyncio
    # primitives (jobkit 2.x creates its task queue eagerly) bind to the event
    # loop that constructed them. A new app instance on a new loop — every
    # test session after the first — would inherit a queue bound to a dead
    # loop and its workers would crash on the first get(). One app start =
    # one fresh orchestrator; in production (single startup per process) this
    # is the first call anyway.
    get_async_orchestrator.cache_clear()
    orchestrator = get_async_orchestrator()
    notifier = WebsocketNotifier(orchestrator)
    orchestrator.bind_notifier(notifier)

    if docling_serve_settings.upload_staging_mode == "required":
        await asyncio.to_thread(check_upload_staging_capability, force=True)

    # Warm up processing cache (loads ML models for LocalOrchestrator;
    # no-op for RQOrchestrator since models live in the worker pods).
    if docling_serve_settings.load_models_at_boot:
        await orchestrator.warm_up_caches()

    _models_ready.set()

    # A fresh app instance starts healthy: the failure latch belongs to THIS
    # instance's queue loop. Without the reset, a prior instance in the same
    # process (test sessions create several) leaks its dead-loop flag into
    # every later instance's readiness probes.
    _queue_processor_failed.clear()

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
    staging_reconciler_task = None
    if docling_serve_settings.upload_staging_mode == "required":
        staging_reconciler_task = asyncio.create_task(_run_staging_cleanup_reconciler())

    yield

    # Cancel the background queue processor on shutdown
    queue_task.cancel()
    if reaper_task:
        reaper_task.cancel()
    if staging_reconciler_task:
        staging_reconciler_task.cancel()
    try:
        await queue_task
    except asyncio.CancelledError:
        _log.info("Queue processor cancelled.")
    if reaper_task:
        try:
            await reaper_task
        except asyncio.CancelledError:
            _log.info("Zombie reaper cancelled.")
    if staging_reconciler_task:
        try:
            await staging_reconciler_task
        except asyncio.CancelledError:
            _log.info("Staging cleanup reconciler cancelled.")

    # Remove scratch directory only when it was an auto-created tempdir
    # (scratch_path unset). Never delete an operator-configured scratch_path.
    if docling_serve_settings.scratch_path is None:
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

    if docling_serve_settings.auth_mode == "assertion":
        require_auth = MachineAssertionAuth(
            issuer=docling_serve_settings.assertion_issuer,
            audience=docling_serve_settings.assertion_audience,
            client_id=docling_serve_settings.assertion_client_id,
            algorithm=docling_serve_settings.assertion_algorithm,
            public_key=docling_serve_settings.assertion_public_key,
            kms_key_id=docling_serve_settings.assertion_kms_key_id,
            kms_region=docling_serve_settings.assertion_kms_region,
            redis_url=docling_serve_settings.assertion_redis_url,
            request_tenant_header=docling_serve_settings.eng_ray_tenant_id_header,
        )
    elif docling_serve_settings.auth_mode == "api_key":
        require_auth = APIKeyAuth(
            docling_serve_settings.api_key,
            allow_no_auth=docling_serve_settings.allow_no_auth,
        )
    else:
        require_auth = APIKeyAuth("", allow_no_auth=True)
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
                    "detail": api_composition.build_public_http_detail(
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

    # Credentials cannot be combined with a wildcard origin: Starlette would
    # otherwise reflect any Origin and return Access-Control-Allow-Credentials,
    # granting credentialed cross-origin access to every site. Only allow
    # credentials when the operator pins an explicit origin allowlist.
    allow_credentials = "*" not in origins

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=allow_credentials,
        allow_methods=methods,
        allow_headers=headers,
    )

    # Mount the Gradio app
    if docling_serve_settings.enable_ui:
        try:
            import gradio as gr

            from docling_serve.gradio_ui import ui as gradio_ui
            from docling_serve.settings import uvicorn_settings

            tmp_output_dir = api_composition.get_scratch() / "gradio"
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

    deps = api_composition.ApiDependencies(
        settings=docling_serve_settings,
        require_auth=require_auth,
        service_policy=service_policy,
        convert_sources_request_model=ConvertSourcesRequestModel,
        default_target_name=default_target_name,
        models_ready=_models_ready,
        queue_processor_failed=_queue_processor_failed,
        orchestrator_provider=lambda: get_async_orchestrator(),
        staging_capability_checker=lambda: check_upload_staging_capability(),
        upload_stager_builder=lambda: build_upload_stager(),
    )
    api_composition.compose_api(
        app,
        deps,
        settings=docling_serve_settings,
        offline_docs_assets=offline_docs_assets,
        downgrade_openapi=downgrade_openapi31_to_30,
    )

    return app
