from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from docling.datamodel.base_models import OutputFormat
from docling.datamodel.document import ConversionResult, ConversionStatus
from docling.datamodel.service.callbacks import (
    DocumentCompletedItem,
    FailedDocsItem,
    ProgressDocumentCompleted,
    ProgressSetNumDocs,
    ProgressUpdateProcessed,
    SucceededDocsItem,
)
from docling_jobkit.convert.results import (
    _export_documents_as_files,
    process_export_results,
)
from docling_jobkit.datamodel.result import DoclingTaskResult, RemoteTargetResult
from docling_jobkit.datamodel.task import Task

from docling_serve.deep_document.options import deep_extraction_mode
from docling_serve.deep_document.s3_publisher import (
    resolve_deep_target,
    upload_tree,
)
from docling_serve.extraction.service import assemble_document_bundle
from docling_serve.extractors import ExtractionContext, select_registry_extractor
from docling_serve.identity import bind_identity, identity_from_task_metadata

if TYPE_CHECKING:
    from docling_jobkit.orchestrators.callback_invoker import CallbackInvoker

_log = logging.getLogger(__name__)


def deep_extraction_requested(task: Task) -> bool:
    """True when the caller submitted the file with ``extraction=deep``."""
    metadata = getattr(task, "metadata", None) or {}
    return deep_extraction_mode(metadata.get("extraction"))


def task_tenant_id(task: Task) -> str | None:
    metadata = getattr(task, "metadata", None) or {}
    tenant_id = metadata.get("tenant_id")
    return str(tenant_id) if tenant_id else None


def task_request_bucket(task: Task) -> str:
    """S3 bucket the caller passed via the ``deep_s3_bucket`` form field."""
    metadata = getattr(task, "metadata", None) or {}
    return str(metadata.get("deep_s3_bucket") or "")


def task_request_prefix(task: Task) -> str:
    """S3 key prefix the caller passed via the ``deep_s3_prefix`` form field."""
    metadata = getattr(task, "metadata", None) or {}
    return str(metadata.get("deep_s3_prefix") or "")


def task_profile(task: Task) -> str:
    """Extraction profile the caller passed via the ``profile`` form field.

    Selects the domain extractor (e.g. ``schematic``, ``access``, ``auto``);
    defaults to ``default`` (generic Docling structure).
    """
    metadata = getattr(task, "metadata", None) or {}
    return str(metadata.get("profile") or "default")


def task_enhancements(task: Task) -> list[str]:
    """Opt-in enhancers requested via the ``enhancements`` form field.

    Accepts a list or a comma-separated string (e.g. ``image_context``).
    """
    metadata = getattr(task, "metadata", None) or {}
    raw = metadata.get("enhancements")
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    return []


def task_source_dir(task: Task) -> Path | None:
    """Scratch dir holding the raw uploaded bytes (set by the file endpoint).

    Docling converts from an in-memory stream, so ``conv_res.input.file`` is a
    bare name with no bytes on disk. Native extractors (python-pptx) re-open the
    true file from this dir by filename.
    """
    metadata = getattr(task, "metadata", None) or {}
    raw = metadata.get("deep_source_dir")
    return Path(str(raw)) if raw else None


def task_legacy_sources(task: Task) -> dict[str, str]:
    """Converted -> original filename for LibreOffice-pre-converted uploads.

    Set by the file endpoint when a legacy ``.doc``/``.xls``/``.ppt`` upload was
    pre-converted to its modern equivalent, so results report the user's
    original filename.
    """
    metadata = getattr(task, "metadata", None) or {}
    raw = metadata.get("legacy_office_sources")
    if isinstance(raw, dict):
        return {str(key): str(value) for key, value in raw.items()}
    return {}


def process_export_results_with_deep_document(
    task: Task,
    conv_results: Iterable[ConversionResult],
    work_dir: Path,
    callback_invoker: CallbackInvoker | None = None,
) -> DoclingTaskResult:
    if not deep_extraction_requested(task):
        return process_export_results(
            task=task,
            conv_results=conv_results,
            work_dir=work_dir,
            callback_invoker=callback_invoker,
        )

    # Deep extraction publishes an S3 expanded object tree. The destination
    # comes from the request (deep_s3_bucket / deep_s3_prefix) or the server
    # default; fail explicitly when neither supplies a bucket. No ZIP fallback.
    bucket, prefix = resolve_deep_target(
        task_id=task.task_id,
        tenant_id=task_tenant_id(task),
        request_bucket=task_request_bucket(task),
        request_prefix=task_request_prefix(task),
    )

    conversion_options = task.convert_options
    if conversion_options is None:
        raise RuntimeError("process_export_results called without task.convert_options")

    # Live stage log at the bundle prefix so clients can watch the extraction
    # work (rendering, model passes, net tracing, …) instead of a blind spinner.
    from docling_serve.deep_document.progress import S3ProgressReporter

    try:
        progress = S3ProgressReporter(bucket=bucket, prefix=prefix, task_id=task.task_id)
    except Exception:
        progress = None
    if progress:
        progress("converting")

    start_time = time.monotonic()
    conv_results_list = collect_conversion_results(
        task=task,
        conv_results=conv_results,
        callback_invoker=callback_invoker,
    )
    processing_time = time.monotonic() - start_time

    _log.info(
        "Processed %s docs in %.2f seconds.",
        len(conv_results_list),
        processing_time,
    )
    if not conv_results_list:
        raise RuntimeError("No documents were generated by Docling.")

    # docling writes its conversions (md / html / json / images) into raw_dir;
    # the standard bundle is assembled into output_dir, which is the S3 upload
    # root. One document per upload (notebook) => bundle at the prefix root.
    raw_dir = work_dir / "raw"
    output_dir = work_dir / "output"
    raw_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    num_succeeded, num_failed, _conv_result = _export_documents_as_files(
        conv_results=conv_results_list,
        output_dir=raw_dir,
        export_json=OutputFormat.JSON in conversion_options.to_formats,
        export_html=OutputFormat.HTML in conversion_options.to_formats,
        export_md=OutputFormat.MARKDOWN in conversion_options.to_formats,
        export_txt=OutputFormat.TEXT in conversion_options.to_formats,
        export_doctags=OutputFormat.DOCTAGS in conversion_options.to_formats,
        image_export_mode=conversion_options.image_export_mode,
        md_page_break_placeholder=conversion_options.md_page_break_placeholder,
    )

    if progress:
        progress("extracting")
    # Re-bind the caller identity captured at the HTTP layer so every model
    # call made by extractors/enhancers below is attributed to the
    # originating user/tenant in LiteLLM spend logs.
    identity = identity_from_task_metadata(getattr(task, "metadata", None))
    with bind_identity(identity):
        manifests = assemble_bundles(
            conv_results=conv_results_list,
            raw_dir=raw_dir,
            output_dir=output_dir,
            task_id=task.task_id,
            source_dir=task_source_dir(task),
            profile=task_profile(task),
            enhancements=task_enhancements(task),
            progress=progress,
            original_names=task_legacy_sources(task),
        )

    # Documents docling could not convert but a registry extractor bundled
    # natively (e.g. an Access DB via mdbtools) count as succeeded.
    recovered = max(0, len(manifests) - num_succeeded)
    if recovered:
        num_succeeded += recovered
        num_failed = max(0, num_failed - recovered)

    if not any(output_dir.iterdir()):
        raise RuntimeError("No documents were exported.")

    if progress:
        progress("publishing")
    published = upload_tree(root_dir=output_dir, bucket=bucket, prefix=prefix)
    _log.info(
        "Published %s deep-document object(s) to s3://%s/%s",
        len(published),
        bucket,
        prefix,
    )
    if progress:
        progress("published", {"objects": len(published)})
        progress.complete()

    source_dir = task_source_dir(task)
    if source_dir is not None:
        shutil.rmtree(source_dir, ignore_errors=True)

    return DoclingTaskResult(
        result=RemoteTargetResult(),
        processing_time=processing_time,
        num_succeeded=num_succeeded,
        num_failed=num_failed,
        num_converted=len(conv_results_list),
    )


def collect_conversion_results(
    *,
    task: Task,
    conv_results: Iterable[ConversionResult],
    callback_invoker: CallbackInvoker | None,
) -> list[ConversionResult]:
    total_docs = len(task.sources)
    if callback_invoker and task.callbacks and total_docs:
        callback_invoker.invoke_callbacks_async(
            callbacks=task.callbacks,
            task_id=task.task_id,
            progress=ProgressSetNumDocs(num_docs=total_docs),
        )

    results: list[ConversionResult] = []
    docs_succeeded: list[SucceededDocsItem] = []
    docs_failed: list[FailedDocsItem] = []
    for idx, conv_res in enumerate(conv_results):
        results.append(conv_res)
        if conv_res.status == ConversionStatus.SUCCESS:
            docs_succeeded.append(SucceededDocsItem(source=str(conv_res.input.file)))
        else:
            docs_failed.append(
                FailedDocsItem(
                    source=str(conv_res.input.file),
                    error=str(conv_res.errors) if conv_res.errors else "Unknown error",
                )
            )

        if callback_invoker and task.callbacks:
            document_info = DocumentCompletedItem(
                source=str(conv_res.input.file),
                status=conv_res.status,
                num_pages=(len(conv_res.document.pages) if conv_res.document else None),
                processing_time=(
                    sum(sum(item.times) for item in conv_res.timings.values())
                    if conv_res.timings
                    else None
                ),
                doc_hash=conv_res.input.document_hash,
                error=str(conv_res.errors) if conv_res.errors else None,
            )
            callback_invoker.invoke_callbacks_async(
                callbacks=task.callbacks,
                task_id=task.task_id,
                progress=ProgressDocumentCompleted(
                    document=document_info,
                    total_processed=idx + 1,
                    total_docs=total_docs,
                ),
            )

    if callback_invoker and task.callbacks:
        callback_invoker.invoke_callbacks_async(
            callbacks=task.callbacks,
            task_id=task.task_id,
            progress=ProgressUpdateProcessed(
                num_processed=len(docs_succeeded) + len(docs_failed),
                num_succeeded=len(docs_succeeded),
                num_failed=len(docs_failed),
                docs_succeeded=docs_succeeded,
                docs_failed=docs_failed,
            ),
        )

    return results


def assemble_bundles(
    *,
    conv_results: Iterable[Any],
    raw_dir: Path,
    output_dir: Path,
    task_id: str,
    source_dir: Path | None = None,
    profile: str = "default",
    enhancements: list[str] | None = None,
    progress: Any | None = None,
    original_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Assemble one standard bundle per extractable document.

    A document is extractable when docling converted it successfully OR a
    registry extractor reads the source natively without docling (e.g. an
    Access DB via mdbtools) — the gate is "an extractor produced units", not
    ``ConversionStatus.SUCCESS``. A single-document upload (the notebook case)
    writes the bundle at the prefix root; multi-document tasks nest each bundle
    under the source stem. The ``progress`` sink (single-document case only —
    multi-doc stages would interleave) is handed to the extractor so domain
    stages stream live. ``original_names`` maps converted -> original filename
    for LibreOffice-pre-converted legacy uploads.
    """
    eligible = [
        conv_res
        for conv_res in conv_results
        if (
            conv_res.status == ConversionStatus.SUCCESS
            and conv_res.document is not None
        )
        or registry_extractor_owns(
            conv_res, task_id=task_id, source_dir=source_dir, profile=profile
        )
    ]
    single = len(eligible) == 1
    manifests: list[dict[str, Any]] = []
    for conv_res in eligible:
        source_path = Path(str(conv_res.input.file))
        bundle_dir = output_dir if single else output_dir / source_path.stem
        manifest = assemble_document_bundle(
            conv_res=conv_res,
            source_path=source_path,
            raw_dir=raw_dir,
            bundle_dir=bundle_dir,
            task_id=task_id,
            single_document=single,
            source_dir=source_dir,
            profile=profile,
            enhancements=enhancements,
            progress=progress if single else None,
            original_name=(original_names or {}).get(source_path.name),
        )
        manifests.append(manifest)
    return manifests


def registry_extractor_owns(
    conv_res: Any,
    *,
    task_id: str,
    source_dir: Path | None,
    profile: str,
) -> bool:
    """True when a non-docling registry extractor owns this document.

    Lets bundle assembly accept sources docling skipped or failed (no backend)
    that an extractor reads natively from the raw bytes. Extractors that need
    a docling conversion (``requires_docling``) never recover a failed one.
    """
    source_path = Path(str(conv_res.input.file))
    probe = ExtractionContext(
        source_path=source_path,
        bundle_dir=source_path.parent,
        media_dir=source_path.parent,
        source_manifest_key=f"task:{task_id}:{source_path.stem}",
        task_id=task_id,
        profile=profile,
        conv_res=conv_res,
        source_dir=source_dir,
    )
    extractor = select_registry_extractor(probe)
    return extractor is not None and not extractor.requires_docling
