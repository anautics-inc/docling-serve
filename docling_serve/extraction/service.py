"""Typed dispatch + standard-bundle assembly for the extraction service.

Every file type lands in the same bundle. Only the *structured* ``document.json``
differs per type: presentations carry per-slide geometry (python-pptx) while
documents carry reading-order sections (docling). ``document.md`` /
``document.html`` always come from docling's conversion so the downstream
pipeline has one consistent thing to chunk.
"""

from __future__ import annotations

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docling_serve.deep_document.artifact_writer import write_json
from docling_serve.deep_document.docling_adapter import source_kind
from docling_serve.deep_document.document_builder import attach_exported_assets
from docling_serve.extractors import ExtractionContext, select_extractor
from docling_serve.extractors.enhancers import run_enhancements

_log = logging.getLogger(__name__)

IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".tif",
    ".tiff",
    ".bmp",
    ".svg",
}


def assemble_document_bundle(
    *,
    conv_res: Any,
    source_path: Path,
    raw_dir: Path,
    bundle_dir: Path,
    task_id: str,
    single_document: bool,
    source_dir: Path | None = None,
    profile: str = "default",
    enhancements: list[str] | None = None,
    progress: Any | None = None,
    original_name: str | None = None,
) -> dict[str, Any]:
    """Assemble the standard bundle for one document into ``bundle_dir``.

    The structured ``document.json`` plus any domain artifacts (schematic graph
    / netlist, Access table CSVs) are produced by the extractor selected from
    the registry for ``(profile, source_path, conv_res)``. Opt-in ``enhancements``
    (e.g. ``image_context``) then enrich the document in place. ``raw_dir`` holds
    docling's exported files (``<stem>.md`` / ``.html`` / images). Returns the
    ``extraction.json`` manifest dict (also written to disk). ``original_name``
    is the user's upload name when the source was pre-converted (legacy Office
    via LibreOffice) so the manifest reports the original filename.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    media_dir = bundle_dir / "media"
    stem = source_path.stem
    source_manifest_key = f"task:{task_id}:{stem}"

    ctx = ExtractionContext(
        source_path=source_path,
        bundle_dir=bundle_dir,
        media_dir=media_dir,
        source_manifest_key=source_manifest_key,
        task_id=task_id,
        profile=profile,
        conv_res=conv_res,
        source_dir=source_dir,
        enhancements=list(enhancements or []),
        progress=progress,
    )
    extractor = select_extractor(ctx)
    result = extractor.build(ctx)
    structured = result.structured

    document_json = bundle_dir / "document.json"
    write_json(document_json, structured)

    # docling-exported markdown / html for this document.
    markdown_path = copy_first(
        raw_dir, bundle_dir / "document.md", suffix=".md", stem=stem, single=single_document
    )
    html_path = copy_first(
        raw_dir, bundle_dir / "document.html", suffix=".html", stem=stem, single=single_document
    )

    # Gather any docling-exported images into the bundle media dir, then let the
    # asset attacher register everything present (ppt media is already there).
    gather_media(raw_dir=raw_dir, media_dir=media_dir, stem=stem, single=single_document)
    attach_exported_assets(deep_document_path=document_json, package_root=bundle_dir)

    # Optional enrichment passes run on the final document (assets attached) and
    # may write context back into it plus emit sidecars.
    enhancement_artifacts: list[str] = []
    enhancement_manifest: dict[str, Any] = {}
    enhancement_notes: list[str] = []
    if ctx.enhancements:
        document = load_json(document_json)
        for enhancement in run_enhancements(ctx, document, base_result=result):
            enhancement_artifacts.extend(enhancement.artifacts)
            enhancement_notes.extend(enhancement.notes)
            for key, value in enhancement.manifest_extra.items():
                enhancement_manifest[key] = value
        write_json(document_json, document)

    media_files = (
        sorted(p.relative_to(bundle_dir).as_posix() for p in media_dir.glob("*"))
        if media_dir.exists()
        else []
    )
    units = (structured.get("document") or {}).get("units") or []

    manifest: dict[str, Any] = {
        "schemaVersion": "1.0",
        "artifactKind": "captify.extraction.v1",
        "taskId": task_id,
        "createdAt": datetime.now(UTC).isoformat(),
        "profile": profile,
        "enhancements": list(ctx.enhancements),
        "source": {
            "originalFileName": original_name or source_path.name,
            "fileKind": source_kind(source_path.name),
        },
        "extractor": result.extractor,
        "domain": result.domain,
        "files": {
            "document": "document.json",
            "markdown": "document.md" if markdown_path else None,
            "html": "document.html" if html_path else None,
        },
        "media": media_files,
        "artifacts": [*result.artifacts, *enhancement_artifacts],
        "counts": {"units": len(units), "media": len(media_files)},
    }
    if original_name and original_name != source_path.name:
        manifest["source"]["convertedFileName"] = source_path.name
    notes = [*result.notes, *enhancement_notes]
    if notes:
        manifest["notes"] = notes
    for key, value in result.manifest_extra.items():
        manifest[key] = value
    for key, value in enhancement_manifest.items():
        manifest[key] = value
    write_json(bundle_dir / "extraction.json", manifest)
    return manifest


def copy_first(
    raw_dir: Path,
    target: Path,
    *,
    suffix: str,
    stem: str,
    single: bool,
) -> Path | None:
    candidate = raw_dir / f"{stem}{suffix}"
    if candidate.is_file():
        shutil.copyfile(candidate, target)
        return target
    if single:
        matches = sorted(raw_dir.glob(f"*{suffix}"))
        if matches:
            shutil.copyfile(matches[0], target)
            return target
    return None


def gather_media(
    *, raw_dir: Path, media_dir: Path, stem: str, single: bool
) -> None:
    images = [
        path
        for path in raw_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    ]
    if not images:
        return
    if not single:
        images = [path for path in images if path.stem.startswith(stem) or stem in path.parts[-2:]]
    if not images:
        return
    media_dir.mkdir(parents=True, exist_ok=True)
    for path in images:
        target = media_dir / path.name
        if not target.exists():
            shutil.copyfile(path, target)


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    return value if isinstance(value, dict) else {}
