from __future__ import annotations

from io import BytesIO

import pytest

from docling.datamodel.base_models import DocumentStream
from docling.datamodel.service.responses import ChunkedDocumentResult
from docling.datamodel.service.tasks import TaskType
from docling_jobkit.datamodel.result import DoclingTaskResult
from docling_jobkit.datamodel.task import Task

from docling_serve.capabilities import (
    CAPABILITIES,
    PROFILE_TO_DOMAIN,
    capability_for_filename,
    classify_document,
)
from docling_serve.ingestion.adapters import ADAPTERS
from docling_serve.ingestion.canonical_result import (
    CANONICAL_CONTRACT,
    CanonicalChunk,
    CanonicalTypedMetadata,
    canonical_from_task_result,
)
from docling_serve.ingestion.canonical_task import (
    finalize_canonical_task,
    prepare_canonical_task,
)

EXPECTED_EXTENSIONS = {
    "document": {
        ".adoc",
        ".asc",
        ".asciidoc",
        ".bmp",
        ".csv",
        ".docm",
        ".docx",
        ".dotm",
        ".dotx",
        ".htm",
        ".html",
        ".jpeg",
        ".jpg",
        ".md",
        ".pdf",
        ".png",
        ".potm",
        ".potx",
        ".ppsm",
        ".ppsx",
        ".pptm",
        ".pptx",
        ".qmd",
        ".rmd",
        ".text",
        ".tif",
        ".tiff",
        ".txt",
        ".webp",
        ".xhtml",
        ".xlsm",
        ".xlsx",
    },
    "legacy-office": {".doc", ".ppt", ".xls"},
    "access": {".accdb", ".mdb"},
    "form": {".pdf"},
    "technical-order": {".pdf"},
    "schematic": {".jpeg", ".jpg", ".pdf", ".png", ".svg", ".tif", ".tiff"},
    "graph-extraction": set(),
}
DIRECT_DOMAINS = ("document", "legacy-office", "access")
TYPED_DOMAINS = ("form", "technical-order", "schematic")
GOLDEN_CASES = [
    (domain, extension, "auto")
    for domain in DIRECT_DOMAINS
    for extension in sorted(EXPECTED_EXTENSIONS[domain])
] + [
    (domain, extension, sorted(CAPABILITIES[domain].profiles)[0])
    for domain in TYPED_DOMAINS
    for extension in sorted(EXPECTED_EXTENSIONS[domain])
]


def test_reviewed_extension_matrix_matches_executable_registry() -> None:
    assert {
        name: set(capability.extensions) for name, capability in CAPABILITIES.items()
    } == (EXPECTED_EXTENSIONS)
    assert set(ADAPTERS) == set(EXPECTED_EXTENSIONS)


@pytest.mark.parametrize(
    ("domain", "extension"),
    [
        (domain, extension)
        for domain in ("document", "legacy-office", "access")
        for extension in sorted(EXPECTED_EXTENSIONS[domain])
    ],
)
def test_every_directly_admitted_extension_resolves_one_domain(
    domain: str, extension: str
) -> None:
    capability = capability_for_filename(f"matrix{extension}")
    assert capability is not None
    assert capability.name == domain


@pytest.mark.parametrize(
    ("profile", "domain"),
    sorted(PROFILE_TO_DOMAIN.items()),
)
def test_every_typed_profile_uses_the_same_registry(profile: str, domain: str) -> None:
    decision = classify_document(
        filename="matrix.pdf",
        payload=b"%PDF-1.7",
        profile=profile,
    )
    assert decision.domain == domain
    assert ADAPTERS[domain].capability is CAPABILITIES[domain]


@pytest.mark.parametrize(
    ("domain", "extension", "profile"),
    GOLDEN_CASES,
)
def test_every_admitted_extension_produces_the_canonical_contract(
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    extension: str,
    profile: str,
) -> None:
    monkeypatch.setattr(
        "docling_serve.access.access_to_markdown",
        lambda _path: ("# Access", []),
    )
    monkeypatch.setattr("docling_serve.access.extract.dump_schema", lambda _path: "")
    monkeypatch.setattr("docling_serve.access.extract._load", lambda _path: object())
    monkeypatch.setattr("docling_serve.access.extract._user_tables", lambda _db: [])
    monkeypatch.setattr(
        "docling_serve.ingestion.canonical_task._markdown_from_result",
        lambda _result: "# Golden document",
    )
    monkeypatch.setattr(
        "docling_serve.ingestion.canonical_task._normalized_chunks",
        lambda _result, filename: [
            CanonicalChunk(filename=filename, chunk_index=0, text="Golden document")
        ],
    )
    monkeypatch.setattr(
        "docling_serve.ingestion.canonical_task._typed_metadata",
        lambda context, **_kwargs: CanonicalTypedMetadata(
            domain=context.decision.domain,
            status="done" if context.decision.domain in TYPED_DOMAINS else "skipped",
            outputContract=CAPABILITIES[context.decision.domain].output_contract,
        ),
    )
    task = Task(
        task_id=f"golden-{domain}-{extension[1:]}",
        task_type=TaskType.CHUNK,
        sources=[
            DocumentStream(
                name=f"golden{extension}",
                stream=BytesIO(b"%PDF-1.7\n/XFA" if domain == "form" else b"golden"),
            )
        ],
        metadata={
            "tenant_id": "golden",
            "canonical_ingestion": {"profile": profile, "ocr_policy": "auto"},
        },
    )
    raw_result = DoclingTaskResult(
        num_converted=1,
        num_succeeded=1,
        num_failed=0,
        processing_time=0.1,
        result=ChunkedDocumentResult(chunks=[], documents=[]),
    )

    with prepare_canonical_task(task) as prepared:
        assert prepared is not None
        result = finalize_canonical_task(prepared, raw_result)

    canonical = canonical_from_task_result(result)
    assert canonical is not None
    assert canonical.contract == CANONICAL_CONTRACT
    assert canonical.routing.domain == domain
    assert canonical.markdown == "# Golden document"
    assert [chunk.text for chunk in canonical.chunks] == ["Golden document"]
    assert canonical.typed is not None
    assert canonical.typed.output_contract == CAPABILITIES[domain].output_contract
