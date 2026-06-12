from __future__ import annotations

import importlib.util
import shutil
import sys
from dataclasses import dataclass

from fastapi import HTTPException, status

from docling.datamodel.service.options import ConvertDocumentsOptions
from docling.datamodel.service.requests import (
    BaseChunkDocumentsRequest,
    ConvertDocumentsRequest,
    S3SourceRequest,
)
from docling.datamodel.service.targets import S3Target
from docling.models.factories import get_ocr_factory

from docling_serve.settings import AsyncEngine, DoclingServeSettings


@dataclass(frozen=True, slots=True)
class ServicePolicy:
    max_document_timeout: float
    allow_external_plugins: bool
    allowed_ocr_presets: frozenset[str]
    s3_enabled: bool
    callbacks_enabled: bool
    custom_vlm_enabled: bool


def build_service_policy(settings: DoclingServeSettings) -> ServicePolicy:
    ocr_factory = get_ocr_factory(
        allow_external_plugins=settings.allow_external_plugins
    )
    registered_ocr_presets = {
        str(kind)
        for kind in ocr_factory.registered_kind
        if ocr_preset_available(str(kind), settings)
    }
    if settings.allowed_ocr_presets is None:
        allowed_ocr_presets = registered_ocr_presets
    else:
        allowed_ocr_presets = set(settings.allowed_ocr_presets) & registered_ocr_presets

    return ServicePolicy(
        max_document_timeout=settings.max_document_timeout,
        allow_external_plugins=settings.allow_external_plugins,
        allowed_ocr_presets=frozenset(allowed_ocr_presets),
        s3_enabled=settings.eng_kind == AsyncEngine.KFP,
        callbacks_enabled=True,
        custom_vlm_enabled=settings.allow_custom_vlm_config,
    )


def ocr_preset_available(preset: str, settings: DoclingServeSettings) -> bool:
    if preset in {"auto", "rapidocr"}:
        return importlib.util.find_spec("rapidocr") is not None
    if preset == "easyocr":
        return importlib.util.find_spec("easyocr") is not None
    if preset == "tesserocr":
        return importlib.util.find_spec("tesserocr") is not None
    if preset == "tesseract":
        return shutil.which("tesseract") is not None
    if preset == "ocrmac":
        return sys.platform == "darwin" and importlib.util.find_spec("ocrmac") is not None
    if preset == "kserve_v2_ocr":
        return settings.enable_remote_services
    return True


def normalize_convert_options(
    options: ConvertDocumentsOptions, policy: ServicePolicy
) -> ConvertDocumentsOptions:
    updates = {}

    if options.document_timeout is None:
        updates["document_timeout"] = policy.max_document_timeout

    if not options.do_picture_description:
        updates.update(
            {
                "picture_description_preset": None,
                "picture_description_custom_config": None,
                "picture_description_local": None,
                "picture_description_api": None,
            }
        )

    if not updates:
        return options

    return options.model_copy(update=updates, deep=True)


def normalize_convert_request(
    request: ConvertDocumentsRequest, policy: ServicePolicy
) -> ConvertDocumentsRequest:
    return request.model_copy(
        update={"options": normalize_convert_options(request.options, policy)},
        deep=True,
    )


def validate_convert_options(
    options: ConvertDocumentsOptions, policy: ServicePolicy
) -> None:
    if options.document_timeout is not None:
        if options.document_timeout <= 0:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="document_timeout must be greater than 0.",
            )
        if options.document_timeout > policy.max_document_timeout:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(
                    "document_timeout exceeds the configured maximum "
                    f"of {policy.max_document_timeout} seconds."
                ),
            )

    if options.ocr_preset not in policy.allowed_ocr_presets:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                f"ocr_preset '{options.ocr_preset}' is not allowed. "
                f"Allowed values: {sorted(policy.allowed_ocr_presets)}."
            ),
        )

    if options.vlm_pipeline_custom_config and not policy.custom_vlm_enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Custom VLM configuration is disabled by server policy.",
        )


def validate_convert_request(
    request: ConvertDocumentsRequest, policy: ServicePolicy
) -> None:
    validate_convert_options(request.options, policy)

    if request.callbacks and not policy.callbacks_enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Callbacks are disabled by server policy.",
        )

    has_s3_source = any(
        isinstance(source, S3SourceRequest) for source in request.sources
    )
    has_s3_target = isinstance(request.target, S3Target)

    if has_s3_source:
        if not policy.s3_enabled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail='source kind "s3" requires engine kind "KFP".',
            )
        if not has_s3_target:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail='source kind "s3" requires target kind "s3".',
            )

    if has_s3_target and not has_s3_source:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail='target kind "s3" requires source kind "s3".',
        )


def validate_chunk_request(
    request: BaseChunkDocumentsRequest, policy: ServicePolicy
) -> None:
    validate_convert_options(request.convert_options, policy)

    if request.callbacks and not policy.callbacks_enabled:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Callbacks are disabled by server policy.",
        )

    has_s3_source = any(
        isinstance(source, S3SourceRequest) for source in request.sources
    )
    has_s3_target = isinstance(request.target, S3Target)

    if has_s3_source:
        if not policy.s3_enabled:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail='source kind "s3" requires engine kind "KFP".',
            )
        if not has_s3_target:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail='source kind "s3" requires target kind "s3".',
            )

    if has_s3_target and not has_s3_source:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail='target kind "s3" requires source kind "s3".',
        )
