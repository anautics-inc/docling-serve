"""Immutable, domain-focused views over the flat deployment settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal


@dataclass(frozen=True)
class StagingSettings:
    mode: Literal["required", "disabled"]
    bucket: str
    region: str
    endpoint: str
    verify_ssl: bool
    key_prefix: str
    retention_days: int
    cleanup_retention_days: int
    dead_letter_retention_days: int
    claim_retention_days: int
    claim_lease_seconds: float
    max_file_size: int
    kms_key_id: str
    io_timeout_seconds: float
    probe_cache_seconds: float
    cleanup_retries: int
    reconcile_interval_seconds: float
    reconcile_batch_size: int


@dataclass(frozen=True)
class LegacyOfficeSettings:
    enabled: bool
    executable: Path | None
    approved_executable_roots: tuple[Path, ...]
    timeout_seconds: float
    max_input_bytes: int
    max_output_bytes: int
    max_scratch_bytes: int
    max_file_count: int
    fetch_timeout_seconds: float
    max_redirects: int


@dataclass(frozen=True)
class GraphSettings:
    enabled: bool
    base_url: str | None
    api_key: str | None
    model: str
    provider: str
    template: str | None
    contract: str
    structured_output: bool
    max_chars: int
    max_output_tokens: int
    context_limit: int


@dataclass(frozen=True)
class AutoRoutingSettings:
    min_parts_signals: int
    max_pdf_streams: int
    max_stream_output_bytes: int
    max_total_stream_output_bytes: int


@dataclass(frozen=True)
class ArtifactSettings:
    enabled: bool
    endpoint: str
    verify_ssl: bool
    bucket: str
    access_key: str
    secret_key: str
    key_prefix: str
    presign_ttl_seconds: int


@dataclass(frozen=True)
class EngineAdapterSettings:
    kind: Any
    local: Mapping[str, Any]
    rq: Mapping[str, Any]
    ray: Mapping[str, Any]


def _selected(settings: Any, prefix: str) -> Mapping[str, Any]:
    values = {
        name.removeprefix(prefix): getattr(settings, name)
        for name in type(settings).model_fields
        if name.startswith(prefix)
    }
    return MappingProxyType(values)


def staging_settings(settings: Any) -> StagingSettings:
    values = dict(_selected(settings, "upload_staging_"))
    values["max_file_size"] = min(values["max_file_size"], settings.max_file_size)
    return StagingSettings(**values)


def legacy_office_settings(settings: Any) -> LegacyOfficeSettings:
    values = dict(_selected(settings, "legacy_office_"))
    values["approved_executable_roots"] = tuple(values["approved_executable_roots"])
    return LegacyOfficeSettings(**values)


def graph_settings(settings: Any) -> GraphSettings:
    return GraphSettings(
        enabled=settings.graph_extraction_enabled,
        base_url=settings.graph_litellm_base_url or settings.litellm_base_url,
        api_key=settings.graph_litellm_api_key or settings.litellm_api_key,
        model=settings.graph_litellm_model,
        provider=settings.graph_litellm_provider,
        template=settings.graph_extraction_template,
        contract=settings.graph_extraction_contract,
        structured_output=settings.graph_extraction_structured_output,
        max_chars=settings.graph_extraction_max_chars,
        max_output_tokens=settings.graph_extraction_max_output_tokens,
        context_limit=settings.graph_extraction_context_limit,
    )


def auto_routing_settings(settings: Any) -> AutoRoutingSettings:
    return AutoRoutingSettings(
        min_parts_signals=settings.auto_route_min_parts_signals,
        max_pdf_streams=settings.auto_route_max_pdf_streams,
        max_stream_output_bytes=settings.auto_route_max_stream_output_bytes,
        max_total_stream_output_bytes=settings.auto_route_max_total_stream_output_bytes,
    )


def artifact_settings(settings: Any) -> ArtifactSettings:
    return ArtifactSettings(**_selected(settings, "artifact_storage_"))


def engine_adapter_settings(settings: Any) -> EngineAdapterSettings:
    return EngineAdapterSettings(
        kind=settings.eng_kind,
        local=_selected(settings, "eng_loc_"),
        rq=_selected(settings, "eng_rq_"),
        ray=_selected(settings, "eng_ray_"),
    )


def current_staging_settings() -> StagingSettings:
    docling_serve_settings = import_module(
        "docling_serve.settings"
    ).docling_serve_settings
    return docling_serve_settings.staging


def current_legacy_office_settings() -> LegacyOfficeSettings:
    docling_serve_settings = import_module(
        "docling_serve.settings"
    ).docling_serve_settings
    return docling_serve_settings.legacy_office


__all__ = [
    "ArtifactSettings",
    "AutoRoutingSettings",
    "EngineAdapterSettings",
    "GraphSettings",
    "LegacyOfficeSettings",
    "StagingSettings",
    "artifact_settings",
    "auto_routing_settings",
    "current_legacy_office_settings",
    "current_staging_settings",
    "engine_adapter_settings",
    "graph_settings",
    "legacy_office_settings",
    "staging_settings",
]
