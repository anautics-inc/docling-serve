"""Chunk-task processing that accepts non-docling registry extractors.

The stock jobkit ``process_chunk_results`` gates every document on a docling
``ConversionStatus.SUCCESS`` — which sources like an Access database (read
natively via mdbtools) can never produce. This wrapper changes the gate to
"an extractor produced units": documents docling converted are chunked
normally; documents docling skipped/failed are offered to the registry
extractors (:func:`select_registry_extractor`) and, when one produces units,
each unit becomes a chunk (e.g. one chunk per Access table, with table-name +
column-header context) so they index like any document.

Wired in place of ``process_chunk_results`` for the local orchestrator (see
``orchestrator_factory``) and the RQ worker (see ``rq_job_wrapper``).
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from docling.datamodel.document import ConversionResult, ConversionStatus
from docling_jobkit.convert.chunking import (
    DocumentChunkerManager,
    process_chunk_results,
)
from docling_jobkit.datamodel.result import (
    ChunkedDocumentResult,
    ChunkedDocumentResultItem,
    DoclingTaskResult,
    ExportDocumentResponse,
    ExportResult,
)
from docling_jobkit.datamodel.task import Task

from docling_serve.deep_document.export_results import (
    task_legacy_sources,
    task_profile,
    task_source_dir,
)
from docling_serve.extractors import (
    ExtractionContext,
    ExtractorResult,
    select_registry_extractor,
)

if TYPE_CHECKING:
    from docling_jobkit.orchestrators.callback_invoker import CallbackInvoker

_log = logging.getLogger(__name__)


def process_chunk_results_with_extractors(
    task: Task,
    conv_results: Iterable[ConversionResult],
    work_dir: Path,
    chunker_manager: DocumentChunkerManager | None = None,
    callback_invoker: CallbackInvoker | None = None,
) -> DoclingTaskResult:
    """Chunk a task's documents, recovering non-docling sources via extractors.

    Documents with a successful docling conversion flow through the stock
    jobkit chunker unchanged. Documents docling could not convert are handed
    to the registry extractors; when one produces units, those units become
    chunks and the document counts as succeeded. Extractor errors propagate
    (typed, actionable) instead of being masked as a docling failure.
    """
    source_dir = task_source_dir(task)
    try:
        docling_results: list[ConversionResult] = []
        extractor_docs: list[tuple[str, ExtractorResult]] = []
        for conv_res in conv_results:
            if conv_res.status == ConversionStatus.SUCCESS:
                docling_results.append(conv_res)
                continue
            built = _build_with_registry_extractor(
                task=task,
                conv_res=conv_res,
                work_dir=work_dir,
                source_dir=source_dir,
            )
            if built is None:
                # Nothing owns it: keep the stock failure accounting.
                docling_results.append(conv_res)
            else:
                extractor_docs.append(
                    (Path(str(conv_res.input.file)).name, built)
                )

        task_result = process_chunk_results(
            task=task,
            conv_results=docling_results,
            work_dir=work_dir,
            chunker_manager=chunker_manager,
            callback_invoker=callback_invoker,
        )

        if extractor_docs and isinstance(task_result.result, ChunkedDocumentResult):
            for filename, built in extractor_docs:
                chunks = extractor_units_to_chunks(filename=filename, result=built)
                _log.info(
                    "Extractor %s produced %s chunk(s) for %s (docling bypassed).",
                    built.extractor,
                    len(chunks),
                    filename,
                )
                task_result.result.chunks.extend(chunks)
                task_result.result.documents.append(
                    ExportResult(
                        content=ExportDocumentResponse(filename=filename),
                        status=ConversionStatus.SUCCESS,
                    )
                )
            task_result.num_succeeded += len(extractor_docs)
            task_result.num_converted += len(extractor_docs)

        _restore_legacy_filenames(task, task_result)
        return task_result
    finally:
        if source_dir is not None:
            shutil.rmtree(source_dir, ignore_errors=True)


def extractor_units_to_chunks(
    *, filename: str, result: ExtractorResult
) -> list[ChunkedDocumentResultItem]:
    """One chunk per deep-document unit, with structural context.

    For an Access database that is one chunk per table: the text leads with
    the table name and column headers (schema context) followed by the row
    preview, so the chunk indexes like any document section.
    """
    units = (result.structured.get("document") or {}).get("units") or []
    chunks: list[ChunkedDocumentResultItem] = []
    for unit in units:
        text = _unit_text(unit)
        if not text.strip():
            continue
        title = unit.get("title")
        metadata: dict[str, Any] = {"extractor": result.extractor}
        if result.domain:
            metadata["domain"] = result.domain
        if unit.get("unitType"):
            metadata["unitType"] = unit["unitType"]
        table_name = (unit.get("sourceRefs") or {}).get("tableName")
        if table_name:
            metadata["tableName"] = table_name
        if title:
            text = f"{title}\n{text}"
        chunks.append(
            ChunkedDocumentResultItem(
                filename=filename,
                chunk_index=len(chunks),
                text=text,
                headings=[title] if title else None,
                doc_items=[],
                page_numbers=[],
                metadata=metadata,
            )
        )
    return chunks


def _build_with_registry_extractor(
    *,
    task: Task,
    conv_res: ConversionResult,
    work_dir: Path,
    source_dir: Path | None,
) -> ExtractorResult | None:
    """Run the owning registry extractor for a docling-failed document.

    Returns ``None`` when no registry extractor owns the source (the docling
    failure stands). Extractor exceptions propagate: they carry the typed,
    actionable error message the job should fail with.
    """
    source_path = Path(str(conv_res.input.file))
    bundle_dir = work_dir / "extractor-bundles" / source_path.stem
    ctx = ExtractionContext(
        source_path=source_path,
        bundle_dir=bundle_dir,
        media_dir=bundle_dir / "media",
        source_manifest_key=f"task:{task.task_id}:{source_path.stem}",
        task_id=task.task_id,
        profile=task_profile(task),
        conv_res=conv_res,
        source_dir=source_dir,
    )
    extractor = select_registry_extractor(ctx)
    if extractor is None or extractor.requires_docling:
        return None
    bundle_dir.mkdir(parents=True, exist_ok=True)
    return extractor.build(ctx)


def _unit_text(unit: dict[str, Any]) -> str:
    """A unit's plain text (column headers + row preview for table units)."""
    content = unit.get("content") or {}
    plain = (content.get("plainText") or "").strip()
    if plain:
        return plain
    parts: list[str] = []
    for element in content.get("elements") or unit.get("elements") or []:
        text = ((element.get("text") or {}).get("plain") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts)


def _restore_legacy_filenames(task: Task, task_result: DoclingTaskResult) -> None:
    """Report the user's original filename for pre-converted legacy uploads."""
    mapping = task_legacy_sources(task)
    if not mapping or not isinstance(task_result.result, ChunkedDocumentResult):
        return
    for chunk in task_result.result.chunks:
        chunk.filename = mapping.get(chunk.filename, chunk.filename)
    for document in task_result.result.documents:
        original = mapping.get(document.content.filename)
        if original:
            document.content.filename = original
