import pytest
from fastapi import HTTPException

from docling.datamodel.service.options import ConvertDocumentsOptions
from docling.datamodel.service.requests import (
    ConvertDocumentsRequest,
    HttpSourceRequest,
    S3SourceRequest,
)
from docling.datamodel.service.targets import InBodyTarget, S3Target

import docling_serve.policy as policy_module
from docling_serve.datamodel.convert import ConvertDocumentsRequestOptions
from docling_serve.policy import (
    build_service_policy,
    normalize_convert_options,
    normalize_convert_request,
    validate_convert_options,
    validate_convert_request,
)
from docling_serve.settings import AsyncEngine, DoclingServeSettings


def test_convert_options_shim_points_to_shared_type():
    assert ConvertDocumentsRequestOptions is ConvertDocumentsOptions


def test_page_range_serializes_to_json_array():
    options = ConvertDocumentsOptions(page_range=(2, 5))

    assert options.model_dump(mode="json")["page_range"] == [2, 5]


def test_normalize_convert_options_sets_default_timeout():
    policy = build_service_policy(DoclingServeSettings())

    normalized = normalize_convert_options(ConvertDocumentsOptions(), policy)

    assert normalized.document_timeout == policy.max_document_timeout


def test_build_service_policy_filters_unavailable_ocr_presets(monkeypatch):
    monkeypatch.setattr(
        policy_module,
        "ocr_preset_available",
        lambda preset, settings: preset != "easyocr",
    )

    policy = build_service_policy(DoclingServeSettings())

    assert "easyocr" not in policy.allowed_ocr_presets


def test_normalize_convert_options_drops_disabled_picture_description_config():
    policy = build_service_policy(DoclingServeSettings())
    options = ConvertDocumentsOptions(
        do_picture_description=False,
        picture_description_preset="granite_vision",
    )

    normalized = normalize_convert_options(options, policy)
    normalized_data = normalized.model_dump()

    assert normalized.picture_description_preset is None
    assert normalized.picture_description_custom_config is None
    assert normalized_data["picture_description_local"] is None
    assert normalized_data["picture_description_api"] is None


def test_normalize_convert_options_drops_disabled_picture_description_custom_config():
    policy = build_service_policy(DoclingServeSettings())
    options = ConvertDocumentsOptions(
        do_picture_description=False,
        picture_description_custom_config={"kind": "stale"},
    )

    normalized = normalize_convert_options(options, policy)
    normalized_data = normalized.model_dump()

    assert normalized.picture_description_preset is None
    assert normalized.picture_description_custom_config is None
    assert normalized_data["picture_description_local"] is None
    assert normalized_data["picture_description_api"] is None


def test_validate_convert_options_rejects_timeout_above_policy():
    policy = build_service_policy(DoclingServeSettings(max_document_timeout=10))

    with pytest.raises(HTTPException, match="document_timeout exceeds"):
        validate_convert_options(ConvertDocumentsOptions(document_timeout=11), policy)


def test_validate_convert_request_rejects_s3_without_kfp():
    policy = build_service_policy(DoclingServeSettings(eng_kind=AsyncEngine.LOCAL))
    request = ConvertDocumentsRequest(
        options=ConvertDocumentsOptions(),
        sources=[
            S3SourceRequest(
                endpoint="s3.example.com",
                access_key="key",
                secret_key="secret",
                bucket="bucket",
            )
        ],
        target=S3Target(
            endpoint="s3.example.com",
            access_key="key",
            secret_key="secret",
            bucket="bucket",
        ),
    )

    with pytest.raises(
        HTTPException, match='source kind "s3" requires engine kind "KFP"'
    ):
        validate_convert_request(request, policy)


def test_normalize_convert_request_preserves_sources_and_target():
    policy = build_service_policy(DoclingServeSettings())
    request = ConvertDocumentsRequest(
        options=ConvertDocumentsOptions(document_timeout=None),
        sources=[HttpSourceRequest(url="https://example.com/test.pdf", headers={})],
        target=InBodyTarget(),
    )

    normalized = normalize_convert_request(request, policy)

    assert normalized.sources == request.sources
    assert normalized.target == request.target
    assert normalized.options.document_timeout == policy.max_document_timeout
