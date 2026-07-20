"""Authoritative document admission, OCR, and typed-routing policy."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from docling.datamodel.base_models import FormatToExtensions, InputFormat

from docling_serve.ingestion.routing.pdf_signals import looks_like_vector_pdf


class OcrPolicy(StrEnum):
    """Caller intent for OCR without binding clients to one OCR engine."""

    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


DocumentDomain = Literal[
    "document",
    "legacy-office",
    "access",
    "form",
    "technical-order",
    "schematic",
    "graph-extraction",
]


@dataclass(frozen=True, slots=True)
class DocumentCapability:
    name: DocumentDomain
    extensions: frozenset[str]
    media_types: frozenset[str]
    output_contract: str
    default_ocr_policy: OcrPolicy
    runtime_adapter: str | None = None
    profiles: frozenset[str] = frozenset()

    def public_dict(self, *, available: bool = True) -> dict[str, object]:
        return {
            "name": self.name,
            "extensions": sorted(self.extensions),
            "mediaTypes": sorted(self.media_types),
            "outputContract": self.output_contract,
            "defaultOcrPolicy": self.default_ocr_policy.value,
            "runtimeAdapter": self.runtime_adapter,
            "profiles": sorted(self.profiles),
            "available": available,
        }


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    domain: DocumentDomain
    reason: str
    ocr_policy: OcrPolicy

    def public_dict(self) -> dict[str, str]:
        return {
            "domain": self.domain,
            "reason": self.reason,
            "ocrPolicy": self.ocr_policy.value,
        }


def _extensions(input_format: InputFormat) -> frozenset[str]:
    return frozenset(
        f".{suffix.lower()}" for suffix in FormatToExtensions[input_format]
    )


_GENERIC_EXTENSIONS = frozenset().union(
    *(
        _extensions(input_format)
        for input_format in (
            InputFormat.PDF,
            InputFormat.DOCX,
            InputFormat.HTML,
            InputFormat.MD,
            InputFormat.ASCIIDOC,
            InputFormat.IMAGE,
            InputFormat.PPTX,
            InputFormat.XLSX,
            InputFormat.CSV,
        )
    )
)

_CAPABILITIES: Final[tuple[DocumentCapability, ...]] = (
    DocumentCapability(
        name="document",
        extensions=_GENERIC_EXTENSIONS,
        media_types=frozenset(),
        output_contract="docling.convert+chunks.v1",
        default_ocr_policy=OcrPolicy.AUTO,
        profiles=frozenset({"document"}),
    ),
    DocumentCapability(
        name="legacy-office",
        extensions=frozenset({".doc", ".ppt", ".xls"}),
        media_types=frozenset(
            {
                "application/msword",
                "application/vnd.ms-powerpoint",
                "application/vnd.ms-excel",
            }
        ),
        output_contract="docling.convert+chunks.v1",
        default_ocr_policy=OcrPolicy.AUTO,
        runtime_adapter="legacy-office",
    ),
    DocumentCapability(
        name="access",
        extensions=frozenset({".mdb", ".accdb"}),
        media_types=frozenset({"application/x-msaccess"}),
        output_contract="captify.access-markdown.v1",
        default_ocr_policy=OcrPolicy.NEVER,
        runtime_adapter="access",
        profiles=frozenset({"access"}),
    ),
    DocumentCapability(
        name="form",
        extensions=frozenset({".pdf"}),
        media_types=frozenset({"application/pdf"}),
        output_contract="captify.form.v1",
        default_ocr_policy=OcrPolicy.NEVER,
        runtime_adapter="form",
        profiles=frozenset({"xfa", "form", "af-form", "dod-form"}),
    ),
    DocumentCapability(
        name="technical-order",
        extensions=frozenset({".pdf"}),
        media_types=frozenset({"application/pdf"}),
        output_contract="captify.bom.v2",
        default_ocr_policy=OcrPolicy.AUTO,
        runtime_adapter="technical-order",
        profiles=frozenset({"technical-order", "to", "ipb"}),
    ),
    DocumentCapability(
        name="schematic",
        extensions=frozenset(
            {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".svg"}
        ),
        media_types=frozenset(
            {
                "application/pdf",
                "image/png",
                "image/jpeg",
                "image/tiff",
                "image/svg+xml",
            }
        ),
        output_contract="captify.schematic.v1",
        default_ocr_policy=OcrPolicy.AUTO,
        runtime_adapter="schematic",
        profiles=frozenset(
            {
                "schematic",
                "schematics",
                "drawing",
                "drawings",
                "technical-order-schematic",
            }
        ),
    ),
    DocumentCapability(
        name="graph-extraction",
        extensions=frozenset(),
        media_types=frozenset(),
        output_contract="captify.graph-extraction.v1",
        default_ocr_policy=OcrPolicy.NEVER,
        runtime_adapter="graph-extraction",
    ),
)

CAPABILITIES: Final[Mapping[DocumentDomain, DocumentCapability]] = MappingProxyType(
    {capability.name: capability for capability in _CAPABILITIES}
)
PROFILE_TO_DOMAIN: Final[Mapping[str, DocumentDomain]] = MappingProxyType(
    {
        profile: capability.name
        for capability in _CAPABILITIES
        for profile in capability.profiles
    }
)

_PARTS_SIGNALS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"FIGURE\s*&", re.I),
    re.compile(r"PART\s+NUMBER", re.I),
    re.compile(r"\bSMR\b.{0,30}\bCODE\b", re.I | re.S),
    re.compile(r"\bFSCM\b|\bCAGEC?\b", re.I),
    re.compile(r"UNITS\s+PER\s+ASS(?:EMBL)?Y", re.I),
)
_FIGURE_CAPTION: Final[re.Pattern[str]] = re.compile(
    r"(?im)^\s*(?:#+\s*)?Figure\s+\d+(?:-\d+)?[A-Z]?\.?\s*$"
)
_MARKDOWN_IMAGE: Final[re.Pattern[str]] = re.compile(
    r"!\[[^\]]*\]\([^)]*\)|<!--\s*image\s*-->", re.I
)


def parse_ocr_policy(
    value: str | OcrPolicy | None,
    *,
    legacy_do_ocr: bool | None = None,
) -> OcrPolicy:
    """Resolve new policy and legacy boolean without ambiguous precedence."""
    if value is not None and str(value).strip():
        try:
            return OcrPolicy(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError("ocr_policy must be one of: auto, always, never") from exc
    if legacy_do_ocr is True:
        return OcrPolicy.ALWAYS
    if legacy_do_ocr is False:
        return OcrPolicy.NEVER
    return OcrPolicy.AUTO


def capability_for_filename(filename: str) -> DocumentCapability | None:
    suffix = Path(filename).suffix.lower()
    for name in ("legacy-office", "access", "document"):
        capability = CAPABILITIES[name]
        if suffix in capability.extensions:
            return capability
    return None


def classify_document(
    *,
    filename: str,
    payload: bytes,
    profile: str | None = None,
    markdown: str | None = None,
    ocr_policy: str | OcrPolicy | None = None,
    legacy_do_ocr: bool | None = None,
    min_parts_signals: int = 2,
    max_pdf_streams: int = 200,
    max_stream_output_bytes: int = 2_000_000,
    max_total_stream_output_bytes: int = 8_000_000,
) -> RoutingDecision:
    """Classify one source using bounded, deterministic signals."""
    requested = (profile or "auto").strip().lower()
    policy = parse_ocr_policy(ocr_policy, legacy_do_ocr=legacy_do_ocr)
    if requested not in {"", "auto"}:
        domain = PROFILE_TO_DOMAIN.get(requested)
        if domain is None:
            raise ValueError(f"Unknown extraction profile: {requested}")
        return RoutingDecision(domain, "explicit profile", policy)

    suffix = Path(filename).suffix.lower()
    if suffix in CAPABILITIES["access"].extensions:
        return RoutingDecision("access", "Access database extension", OcrPolicy.NEVER)
    if suffix in CAPABILITIES["legacy-office"].extensions:
        return RoutingDecision("legacy-office", "legacy Office extension", policy)
    if suffix == ".pdf" and b"/XFA" in payload:
        return RoutingDecision("form", "XFA AcroForm marker", OcrPolicy.NEVER)
    if suffix == ".pdf" and _looks_like_technical_order(
        markdown, min_signals=min_parts_signals
    ):
        return RoutingDecision(
            "technical-order", "technical-order content signals", policy
        )
    if suffix == ".pdf" and _looks_like_figure_only_document(markdown):
        return RoutingDecision(
            "technical-order", "figure-only technical document", policy
        )
    if suffix == ".pdf" and looks_like_vector_pdf(
        payload,
        max_streams=max_pdf_streams,
        max_stream_output_bytes=max_stream_output_bytes,
        max_total_output_bytes=max_total_stream_output_bytes,
    ):
        return RoutingDecision("schematic", "vector drawing signals", policy)
    if suffix in CAPABILITIES["document"].extensions:
        return RoutingDecision("document", "generic supported format", policy)
    raise ValueError(f"Unsupported document format: {suffix or '<none>'}")


def _looks_like_technical_order(markdown: str | None, *, min_signals: int) -> bool:
    if not markdown:
        return False
    return sum(bool(signal.search(markdown)) for signal in _PARTS_SIGNALS) >= max(
        1, min_signals
    )


def _looks_like_figure_only_document(markdown: str | None) -> bool:
    if not markdown:
        return False
    captions = _FIGURE_CAPTION.findall(markdown)
    if len(captions) < 2:
        return False
    remainder = _FIGURE_CAPTION.sub("", markdown)
    remainder = _MARKDOWN_IMAGE.sub("", remainder)
    remainder = re.sub(r"[\s#*_`-]+", "", remainder)
    return len(remainder) < 200
