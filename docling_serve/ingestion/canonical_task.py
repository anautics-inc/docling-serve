"""Preparation and result decoration owned by the asynchronous task worker."""

from __future__ import annotations

import base64
import json
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any

from docling.datamodel.base_models import DocumentStream
from docling_jobkit.datamodel.result import DoclingTaskResult
from docling_jobkit.datamodel.task import Task

from docling_serve.capabilities import CAPABILITIES, RoutingDecision, classify_document
from docling_serve.ingestion.canonical_result import (
    CanonicalChunk,
    CanonicalRouting,
    CanonicalTaskResult,
    CanonicalTypedMetadata,
    attach_canonical_result,
)
from docling_serve.ingestion.typed_domains import (
    extract_access_domain,
    extract_form_domain,
    extract_schematic_domain,
    extract_technical_order_domain,
    public_form_payload,
)

CANONICAL_TASK_METADATA_KEY = "canonical_ingestion"


@dataclass(slots=True)
class CanonicalTaskContext:
    task: Task
    original_name: str
    original_path: Path
    decision: RoutingDecision
    tenant_id: str
    config: dict[str, Any]
    source_metadata: dict[str, Any] | None = None
    typed: CanonicalTypedMetadata | None = None


def is_canonical_task(task: Task) -> bool:
    return isinstance(task.metadata.get(CANONICAL_TASK_METADATA_KEY), dict)


def _source_name_and_bytes(task: Task) -> tuple[str, bytes]:
    if len(task.sources) != 1:
        raise ValueError("Canonical ingestion accepts exactly one source.")
    source = task.sources[0]
    if isinstance(source, DocumentStream):
        position = source.stream.tell()
        source.stream.seek(0)
        payload = source.stream.read()
        source.stream.seek(position)
        return source.name, payload
    encoded = getattr(source, "base64_string", None)
    if isinstance(encoded, str):
        return str(getattr(source, "filename", None) or "document"), base64.b64decode(
            encoded
        )
    raise ValueError("Canonical file task source was not materialized.")


@contextmanager
def prepare_canonical_task(task: Task) -> Iterator[CanonicalTaskContext | None]:
    """Prepare format-specific input before the ordinary convert/chunk pipeline."""

    config = task.metadata.get(CANONICAL_TASK_METADATA_KEY)
    if not isinstance(config, dict):
        yield None
        return

    name, payload = _source_name_and_bytes(task)
    with tempfile.TemporaryDirectory(prefix="canonical-ingestion-") as work:
        original_path = Path(work) / Path(name).name
        original_path.write_bytes(payload)
        decision = classify_document(
            filename=name,
            payload=payload,
            profile=str(config.get("profile") or "auto"),
            ocr_policy=str(config.get("ocr_policy") or "auto"),
        )
        worker_task = task
        source_metadata: dict[str, Any] | None = None
        prepared_stream: BytesIO | None = None
        if decision.domain == "access":
            access_payload = extract_access_domain(original_path, source_key=name)
            markdown = str(access_payload["markdown"])
            prepared_stream = BytesIO(markdown.encode("utf-8"))
            worker_task = task.model_copy(
                update={
                    "sources": [
                        DocumentStream(
                            name=f"{name}.md",
                            stream=prepared_stream,
                        )
                    ]
                }
            )
            source_metadata = {
                key: value for key, value in access_payload.items() if key != "markdown"
            }
        elif decision.domain == "form":
            form_payload = extract_form_domain(original_path, source_key=name)
            markdown = str(form_payload.get("markdown") or "")
            prepared_stream = BytesIO(markdown.encode("utf-8"))
            worker_task = task.model_copy(
                update={
                    "sources": [
                        DocumentStream(
                            name=f"{name}.md",
                            stream=prepared_stream,
                        )
                    ]
                }
            )
        try:
            context = CanonicalTaskContext(
                task=worker_task,
                original_name=name,
                original_path=original_path,
                decision=decision,
                tenant_id=str(task.metadata.get("tenant_id") or "default"),
                config=config,
                source_metadata=source_metadata,
            )
            yield context
        finally:
            if prepared_stream is not None:
                prepared_stream.close()


def _markdown_from_result(task_result: DoclingTaskResult) -> str:
    result = task_result.result
    parts: list[str] = []
    for document in getattr(result, "documents", []) or []:
        content = getattr(document, "content", None)
        markdown = getattr(content, "md_content", None)
        if isinstance(markdown, str) and markdown.strip():
            parts.append(markdown.strip())
            continue
        json_content = getattr(content, "json_content", None)
        if json_content:
            try:
                from docling_core.types.doc import DoclingDocument
                from docling_core.types.doc.document import ContentLayer

                rendered = DoclingDocument.model_validate(
                    json_content
                ).export_to_markdown(
                    included_content_layers={ContentLayer.BODY, ContentLayer.NOTES}
                )
                if rendered.strip():
                    parts.append(rendered.strip())
            except Exception:
                pass
    if parts:
        return "\n\n".join(parts)
    return "\n\n".join(
        str(getattr(chunk, "text", "")).strip()
        for chunk in getattr(result, "chunks", []) or []
        if str(getattr(chunk, "text", "")).strip()
    )


def _normalized_chunks(
    task_result: DoclingTaskResult, filename: str
) -> list[CanonicalChunk]:
    chunks: list[CanonicalChunk] = []
    for index, item in enumerate(getattr(task_result.result, "chunks", []) or []):
        raw = (
            item.model_dump(mode="json") if hasattr(item, "model_dump") else dict(item)
        )
        meta = raw.pop("meta", None)
        metadata = raw.pop("metadata", None)
        if isinstance(meta, dict):
            raw = {**meta, **raw}
        text = raw.get("text")
        if not isinstance(text, str):
            continue
        chunks.append(
            CanonicalChunk(
                filename=str(raw.get("filename") or filename),
                chunk_index=int(raw.get("chunk_index", index)),
                text=text,
                raw_text=raw.get("raw_text")
                if isinstance(raw.get("raw_text"), str)
                else None,
                headings=[str(value) for value in raw.get("headings") or []],
                page_numbers=[
                    int(value)
                    for value in raw.get("page_numbers") or []
                    if isinstance(value, int)
                ],
                doc_items=[str(value) for value in raw.get("doc_items") or []],
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        )
    return chunks


def _typed_metadata(
    context: CanonicalTaskContext,
    *,
    tenant_id: str,
    config: dict[str, Any],
) -> CanonicalTypedMetadata | None:
    domain = context.decision.domain
    if domain in {"document", "legacy-office"}:
        return CanonicalTypedMetadata(domain=domain, status="skipped")
    capability = CAPABILITIES[domain]
    if domain == "access":
        access_summary = dict(context.source_metadata or {})
        access_summary.pop("schema", None)
        return CanonicalTypedMetadata(
            domain=domain,
            status="done",
            outputContract=capability.output_contract,
            summary=access_summary,
        )

    bucket = str(config.get("bucket") or "").strip() or None
    prefix = str(config.get("prefix") or "").strip().strip("/") or None
    published: list[str] = []
    summary: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix=f"canonical-{domain}-") as work:
        bundle = Path(work) / "bundle"
        if domain == "form":
            service_payload = extract_form_domain(
                context.original_path,
                source_key=context.original_name,
                include_packets=True,
            )
            packets = service_payload.get("_packets") or {}
            payload = public_form_payload(service_payload)
            summary = {
                key: payload.get(key)
                for key in ("fieldCount", "labelCount", "boundValueCount", "sections")
            }
            if bucket and prefix:
                bundle.mkdir(parents=True)
                (bundle / "form.json").write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                (bundle / "form.md").write_text(
                    str(payload.get("markdown") or ""), encoding="utf-8"
                )
                (bundle / "xfa-fields.json").write_text(
                    json.dumps(
                        {
                            "source": context.original_name,
                            "fieldCount": payload.get("fieldCount"),
                            "labelCount": payload.get("labelCount"),
                            "boundValueCount": payload.get("boundValueCount"),
                            "fields": payload.get("fields") or [],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                for packet_name in ("template", "datasets"):
                    if raw := packets.get(packet_name):
                        (bundle / f"xfa-{packet_name}.xml").write_bytes(raw)
                (bundle / "extraction.json").write_text(
                    json.dumps(
                        {
                            "domain": "form",
                            "form": {
                                "format": "xfa",
                                "payload": "form.json",
                                "markdown": "form.md",
                                "fieldCatalog": "xfa-fields.json",
                                "template": "xfa-template.xml",
                                "datasets": "xfa-datasets.xml"
                                if payload.get("hasDatasets")
                                else None,
                            },
                            "fieldCount": payload.get("fieldCount"),
                            "sections": payload.get("sections"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        elif domain == "technical-order":
            payload = extract_technical_order_domain(
                context.original_path,
                source_key=context.original_name,
                media_dir=bundle / "media" if bucket and prefix else None,
            )
            summary = {
                key: payload.get(key)
                for key in (
                    "documentNumber",
                    "documentType",
                    "entryCount",
                    "figureCount",
                )
            }
            if (
                bucket
                and prefix
                and (
                    int(payload.get("entryCount") or 0) > 0
                    or int(payload.get("figureCount") or 0) > 0
                )
            ):
                bundle.mkdir(parents=True, exist_ok=True)
                (bundle / "bom.json").write_text(
                    json.dumps(payload.get("bom") or {}, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                (bundle / "extraction.json").write_text(
                    json.dumps(
                        {
                            "domain": domain,
                            "technicalOrder": {"bom": "bom.json"},
                            **summary,
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
        else:
            payload = extract_schematic_domain(
                context.original_path,
                bundle,
                profile=str(config.get("profile") or "schematic"),
                tenant_id=tenant_id,
                source_key=context.original_name,
            )
            graph = payload.get("graph")
            summary = {
                "components": len(graph.get("components") or [])
                if isinstance(graph, dict)
                else 0
            }
        if bucket and prefix and bundle.is_dir():
            from docling_serve.artifacts import publish_dir_to_s3

            published = publish_dir_to_s3(bundle, bucket=bucket, prefix=prefix)
    return CanonicalTypedMetadata(
        domain=domain,
        status="done",
        outputContract=capability.output_contract,
        bucket=bucket if published else None,
        prefix=prefix if published else None,
        artifactKeys=published,
        summary=summary,
    )


def finalize_canonical_task(
    context: CanonicalTaskContext | None,
    task_result: DoclingTaskResult,
) -> DoclingTaskResult:
    """Run typed dispatch and attach one format-neutral result envelope."""

    if context is None:
        return task_result
    markdown = _markdown_from_result(task_result)
    context.decision = classify_document(
        filename=context.original_name,
        payload=context.original_path.read_bytes(),
        profile=str(context.config.get("profile") or "auto"),
        markdown=markdown,
        ocr_policy=str(context.config.get("ocr_policy") or "auto"),
    )
    try:
        context.typed = _typed_metadata(
            context,
            tenant_id=context.tenant_id,
            config=context.config,
        )
    except Exception as exc:
        if str(context.config.get("profile") or "auto").strip().lower() not in {
            "",
            "auto",
        }:
            raise
        context.typed = CanonicalTypedMetadata(
            domain=context.decision.domain,
            status="error",
            outputContract=CAPABILITIES[context.decision.domain].output_contract,
            error=str(exc),
        )
    canonical = CanonicalTaskResult(
        markdown=markdown,
        chunks=_normalized_chunks(task_result, context.original_name),
        routing=CanonicalRouting.model_validate(context.decision.public_dict()),
        typed=context.typed,
        sourceMetadata=context.source_metadata,
        processingTime=task_result.processing_time,
    )
    return attach_canonical_result(task_result, canonical)
