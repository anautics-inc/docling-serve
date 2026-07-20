"""Format-neutral result contract carried by canonical chunk tasks."""

from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from docling_jobkit.datamodel.result import DoclingTaskResult

CANONICAL_CONTRACT: Final[Literal["docling.canonical-ingestion.v1"]] = (
    "docling.canonical-ingestion.v1"
)
CANONICAL_INFO_KEY = "canonical_ingestion"


class CanonicalChunk(BaseModel):
    """One normalized chunk, independent of Docling's concrete result classes."""

    model_config = ConfigDict(extra="allow")

    filename: str
    chunk_index: int
    text: str
    raw_text: str | None = None
    headings: list[str] = Field(default_factory=list)
    page_numbers: list[int] = Field(default_factory=list)
    doc_items: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CanonicalRouting(BaseModel):
    domain: str
    reason: str
    ocr_policy: str = Field(alias="ocrPolicy")

    model_config = ConfigDict(populate_by_name=True)


class CanonicalTypedMetadata(BaseModel):
    """Typed extraction outcome; payloads remain in their existing bundle schemas."""

    domain: str
    status: Literal["done", "skipped", "error"]
    output_contract: str | None = Field(default=None, alias="outputContract")
    bucket: str | None = None
    prefix: str | None = None
    artifact_keys: list[str] = Field(default_factory=list, alias="artifactKeys")
    summary: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None

    model_config = ConfigDict(populate_by_name=True)


class CanonicalTaskResult(BaseModel):
    """Public HTTP envelope produced from one asynchronous Docling task."""

    contract: Literal["docling.canonical-ingestion.v1"] = CANONICAL_CONTRACT
    markdown: str
    chunks: list[CanonicalChunk]
    routing: CanonicalRouting
    typed: CanonicalTypedMetadata | None = None
    source_metadata: dict[str, Any] | None = Field(default=None, alias="sourceMetadata")
    processing_time: float | None = Field(default=None, alias="processingTime")

    model_config = ConfigDict(populate_by_name=True)


def canonical_from_task_result(
    task_result: DoclingTaskResult,
) -> CanonicalTaskResult | None:
    """Read the compatible canonical extension from a jobkit chunk result."""

    result = task_result.result
    info = getattr(result, "chunking_info", None)
    payload = info.get(CANONICAL_INFO_KEY) if isinstance(info, dict) else None
    if not isinstance(payload, dict):
        return None
    return CanonicalTaskResult.model_validate(payload)


def attach_canonical_result(
    task_result: DoclingTaskResult,
    canonical: CanonicalTaskResult,
) -> DoclingTaskResult:
    """Attach the envelope without adding a non-jobkit result discriminator."""

    result = task_result.result
    if not hasattr(result, "chunking_info"):
        raise TypeError("Canonical ingestion requires a chunk task result.")
    info = dict(getattr(result, "chunking_info", None) or {})
    info[CANONICAL_INFO_KEY] = canonical.model_dump(mode="json", by_alias=True)
    result.chunking_info = info
    return task_result
