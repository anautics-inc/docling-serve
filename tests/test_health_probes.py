import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from docling.datamodel.service.responses import (
    HealthCheckResponse,
    ReadinessResponse,
)

from docling_serve.app import (
    _models_ready,
    _queue_processor_failed,
    _supervise_queue_processor,
    create_app,
)
from docling_serve.settings import AsyncEngine, docling_serve_settings
from docling_serve.upload_staging import UploadStagingCapabilityError


@pytest_asyncio.fixture
async def app():
    original_load_models_at_boot = docling_serve_settings.load_models_at_boot
    original_legacy_office_enabled = docling_serve_settings.legacy_office_enabled
    original_staging_mode = docling_serve_settings.upload_staging_mode
    docling_serve_settings.load_models_at_boot = False
    docling_serve_settings.legacy_office_enabled = False
    docling_serve_settings.upload_staging_mode = "disabled"
    try:
        app = create_app()
        async with LifespanManager(app) as manager:
            yield manager.app
    finally:
        docling_serve_settings.load_models_at_boot = original_load_models_at_boot
        docling_serve_settings.legacy_office_enabled = original_legacy_office_enabled
        docling_serve_settings.upload_staging_mode = original_staging_mode


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://app.io"
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_health(client: AsyncClient):
    response = await client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_ready(client: AsyncClient):
    response = await client.get("/ready")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_readyz_alias(client: AsyncClient):
    response = await client.get("/readyz")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_adapter_readiness_reports_runtime_probes(client: AsyncClient):
    expected = {
        "form": True,
        "schematic": False,
        "technical-order": True,
        "access": False,
    }
    with patch(
        "docling_serve.api.health.adapter_readiness",
        return_value=expected,
    ):
        response = await client.get("/ready/adapters")
    assert response.status_code == 200
    assert response.json() == {"adapters": expected}


@pytest.mark.asyncio
async def test_readiness_keeps_generic_service_ready_when_legacy_adapter_missing(
    client: AsyncClient,
):
    original_enabled = docling_serve_settings.legacy_office_enabled
    docling_serve_settings.legacy_office_enabled = True
    try:
        with patch(
            "docling_serve.legacy_office.check_legacy_office_capability",
            side_effect=RuntimeError("unavailable"),
        ):
            response = await client.get("/ready")
            adapters = await client.get("/ready/adapters")
    finally:
        docling_serve_settings.legacy_office_enabled = original_enabled
    assert response.status_code == 200
    assert adapters.status_code == 200
    assert adapters.json()["adapters"]["legacy-office"] is False
    assert adapters.json()["adapters"]["document"] is True


@pytest.mark.asyncio
async def test_readiness_fails_when_required_staging_probe_fails(
    client: AsyncClient,
):
    original_mode = docling_serve_settings.upload_staging_mode
    docling_serve_settings.upload_staging_mode = "required"
    try:
        with patch(
            "docling_serve.app.check_upload_staging_capability",
            side_effect=UploadStagingCapabilityError("unreachable"),
        ):
            response = await client.get("/ready")
    finally:
        docling_serve_settings.upload_staging_mode = original_mode
    assert response.status_code == 503
    assert response.json()["detail"] == "Durable upload staging is unavailable."


@pytest.mark.asyncio
async def test_readyz_for_ray_runs_deep_connection_check(client: AsyncClient):
    original_engine = docling_serve_settings.eng_kind
    original_debug = docling_serve_settings.debug_error_details
    docling_serve_settings.eng_kind = AsyncEngine.RAY
    docling_serve_settings.debug_error_details = False
    try:
        orchestrator = MagicMock()
        orchestrator.check_connection = AsyncMock(
            side_effect=RuntimeError("Ray dispatcher unavailable")
        )

        with patch(
            "docling_serve.app.get_async_orchestrator", return_value=orchestrator
        ):
            response = await client.get("/readyz")
    finally:
        docling_serve_settings.eng_kind = original_engine
        docling_serve_settings.debug_error_details = original_debug

    assert response.status_code == 503
    assert response.json()["detail"] == "Readiness check failed"
    orchestrator.check_connection.assert_awaited_once()


@pytest.mark.asyncio
async def test_readyz_exposes_raw_error_in_debug_mode(client: AsyncClient):
    original_engine = docling_serve_settings.eng_kind
    original_debug = docling_serve_settings.debug_error_details
    docling_serve_settings.eng_kind = AsyncEngine.RAY
    docling_serve_settings.debug_error_details = True
    try:
        orchestrator = MagicMock()
        orchestrator.check_connection = AsyncMock(
            side_effect=RuntimeError("Ray dispatcher unavailable")
        )

        with patch(
            "docling_serve.app.get_async_orchestrator", return_value=orchestrator
        ):
            response = await client.get("/readyz")
    finally:
        docling_serve_settings.eng_kind = original_engine
        docling_serve_settings.debug_error_details = original_debug

    assert response.status_code == 503
    assert response.json()["detail"] == "Ray dispatcher unavailable"


@pytest.mark.asyncio
async def test_livez_alias(client: AsyncClient):
    response = await client.get("/livez")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


@pytest.mark.asyncio
async def test_livez_for_ray_does_not_check_orchestrator_health(
    client: AsyncClient,
):
    original_engine = docling_serve_settings.eng_kind
    docling_serve_settings.eng_kind = AsyncEngine.RAY
    try:
        with patch(
            "docling_serve.app.get_async_orchestrator",
            side_effect=AssertionError("/livez should not touch the Ray orchestrator"),
        ):
            response = await client.get("/livez")
    finally:
        docling_serve_settings.eng_kind = original_engine

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_ready_returns_503_when_models_not_loaded(client: AsyncClient):
    _models_ready.clear()
    try:
        response = await client.get("/ready")
        assert response.status_code == 503
        assert "Models not yet loaded" in response.json()["detail"]
    finally:
        _models_ready.set()


@pytest.mark.asyncio
async def test_livez_returns_503_when_queue_processor_failed(client: AsyncClient):
    _queue_processor_failed.set()
    try:
        response = await client.get("/livez")
        assert response.status_code == 503
        assert "queue processor is not running" in response.json()["detail"]
    finally:
        _queue_processor_failed.clear()


@pytest.mark.asyncio
async def test_ready_returns_503_when_queue_processor_failed(client: AsyncClient):
    _queue_processor_failed.set()
    try:
        response = await client.get("/ready")
        assert response.status_code == 503
        assert "queue processor is not running" in response.json()["detail"]
    finally:
        _queue_processor_failed.clear()


def test_supervise_queue_processor_flags_on_exception():
    event = asyncio.Event()
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = RuntimeError("pub/sub listener died")

    _supervise_queue_processor(task, event)

    assert event.is_set()


def test_supervise_queue_processor_ignores_clean_return():
    # KFP (and other no-op orchestrators) return from process_queue() immediately
    # with no exception; that must not mark the pod unhealthy.
    event = asyncio.Event()
    task = MagicMock()
    task.cancelled.return_value = False
    task.exception.return_value = None

    _supervise_queue_processor(task, event)

    assert not event.is_set()


def test_supervise_queue_processor_ignores_cancellation():
    event = asyncio.Event()
    task = MagicMock()
    task.cancelled.return_value = True

    _supervise_queue_processor(task, event)

    assert not event.is_set()


def test_health_check_response_model():
    resp = HealthCheckResponse()
    assert resp.status == "ok"


def test_readiness_response_model():
    resp = ReadinessResponse()
    assert resp.status == "ok"
