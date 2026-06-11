"""Docling-centric manifest assembly.

Composes the pipeline: DoclingDocument JSON → structural spine → OOXML
enrichment (theme/notes/background/typography) → manifest dict.

This is the new builder. The Docling spine owns structure; OOXML is a
PPTX-only enrichment layer. The same `build_manifest` works for any format
whose DoclingDocument the spine can read — enrichment is simply skipped when
the source is not a PPTX.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from .docling_document import parse_docling_document, spine_summary
from .ooxml_enrichment import attach_typography, enrich_units, enrichment_summary

SCHEMA_VERSION = "3.0"
ARTIFACT_KIND = "deep_document_manifest"
PPTX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# Source extensions the OOXML enrichment layer applies to.
OOXML_ENRICHABLE_SUFFIXES = {".pptx"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "unknown"


def document_id(source_path: Path) -> str:
    return hashlib.sha256(source_path.name.encode("utf-8")).hexdigest()[:16]


def extraction_id(source_sha: str, effective_options: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "sourceSha256": source_sha,
            "schemaVersion": SCHEMA_VERSION,
            "doclingVersion": _package_version("docling"),
            "effectiveOptions": effective_options,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _resolve_picture_path(docling_dir: Path | None, picture_index: int | None) -> Path | None:
    """Locate a picture binary by index in the Docling output's images dir.

    Docling exports referenced images as `images/image_{NNNNNN}_{sha}.png`.
    Index is the stable handle — the JSON `uri` is a stale absolute path.
    """
    if docling_dir is None or picture_index is None:
        return None
    images_dir = docling_dir / "images"
    if not images_dir.is_dir():
        return None
    matches = sorted(images_dir.glob(f"image_{picture_index:06d}_*"))
    return matches[0] if matches else None


def _collect_assets(
    units: list[dict[str, Any]],
    background_assets: dict[str, Any],
    docling_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """Build the manifest asset table from picture blocks + slide backgrounds.

    Picture binaries are resolved by index from the Docling output directory;
    asset ids are content-addressed (the sha embedded in Docling's filename),
    so they are stable across re-extractions. Background images arrive already
    deduped by sha256 from the enrichment layer.
    """
    assets: dict[str, dict[str, Any]] = {}
    for unit in units:
        for block in unit["blocks"]:
            if block["kind"] != "picture":
                continue
            image = block.get("image") or {}
            picture_index = image.get("pictureIndex")
            resolved = _resolve_picture_path(docling_dir, picture_index)

            if resolved is not None:
                # Filename is image_{NNNNNN}_{sha}.png — the sha is content-addressed.
                parts = resolved.stem.split("_", 2)
                content_key = parts[2] if len(parts) == 3 else resolved.stem
                local_path: str | None = str(resolved)
            else:
                content_key = f"idx-{picture_index}"
                local_path = None
            asset_id = f"asset-{hashlib.sha256(content_key.encode('utf-8')).hexdigest()[:12]}"

            block["assetId"] = asset_id
            if asset_id not in assets:
                assets[asset_id] = {
                    "assetId": asset_id,
                    "kind": "picture",
                    "mimeType": image.get("mimeType"),
                    "localPath": local_path,
                    "width": image.get("width"),
                    "height": image.get("height"),
                    "usedBy": [],
                }
            assets[asset_id]["usedBy"].append({"unitId": unit["unitId"], "blockId": block["blockId"]})

    for asset_id, media in background_assets.items():
        if asset_id not in assets:
            assets[asset_id] = {
                "assetId": asset_id,
                "kind": getattr(media, "kind", "slide_background"),
                "mimeType": getattr(media, "mime_type", None),
                "localPath": None,
                "sha256": getattr(media, "sha256", None),
                "sizeBytes": getattr(media, "size_bytes", None),
                "usedBy": [],
            }
    return sorted(assets.values(), key=lambda a: a["assetId"])


def build_manifest(
    source: str | Path,
    docling_json: dict[str, Any],
    *,
    docling_dir: str | Path | None = None,
    docling_json_key: str | None = None,
) -> dict[str, Any]:
    """Assemble the STRUCTURAL manifest from a source file + its DoclingDocument.

    Works for any format Docling can read — `source` is any document, not just
    PPTX (OOXML enrichment is skipped for non-PPTX). `docling_dir` holds
    `document.json` + `images/` for picture-binary resolution.

    NOTE: this returns an *intermediate* manifest WITHOUT `taxonomy` — it is
    not schema-valid on its own. Use `extract_document()` for the complete,
    schema-valid production manifest (structure + captions + Bloom).
    """
    source_path = Path(source)
    started_at = _utc_now()
    errors: list[dict[str, Any]] = []

    units = parse_docling_document(docling_json)

    theme: dict[str, Any] | None = None
    background_assets: dict[str, Any] = {}
    typography_coverage: dict[str, Any] = {}
    enrichable = source_path.suffix.lower() in OOXML_ENRICHABLE_SUFFIXES
    if enrichable:
        try:
            theme, background_assets = enrich_units(units, source_path)
            typography_coverage = attach_typography(units, source_path)
        except Exception as err:  # enrichment must never sink the structural spine
            errors.append(
                {
                    "stage": "ooxml_enrichment",
                    "message": f"{type(err).__name__}: {err}",
                    "recoverable": True,
                }
            )

    source_sha = _sha256_file(source_path)
    # AUDIT F6: record the effective configuration in the manifest so a run is
    # reproducible and no threshold is an invisible literal. Provider IDs are
    # added by extract_document() once captioning/Bloom have run.
    effective_options = {
        "spine": "docling_document",
        "enrichment": "ooxml" if enrichable else "none",
        "doclingJsonKey": docling_json_key,
        "typographyMinSubstringRatio": typography_coverage.get("minSubstringRatio"),
    }
    assets = _collect_assets(
        units, background_assets, Path(docling_dir) if docling_dir else None
    )

    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "artifactKind": ARTIFACT_KIND,
        "documentId": document_id(source_path),
        "documentType": source_path.suffix.lower().lstrip("."),
        "createdAt": _utc_now(),
        "source": {
            "originalFileName": source_path.name,
            "sizeBytes": source_path.stat().st_size,
            "sha256": source_sha,
            "contentType": PPTX_CONTENT_TYPE if enrichable else None,
            "localPath": str(source_path),
        },
        "extraction": {
            "mode": "deep",
            "extractionId": extraction_id(source_sha, effective_options),
            "status": "partial" if errors else "complete",
            "startedAt": started_at,
            "completedAt": _utc_now(),
            "doclingVersion": _package_version("docling"),
            "effectiveOptions": effective_options,
        },
        "theme": theme,
        "units": units,
        "assets": assets,
        "diagnostics": {
            **spine_summary(units),
            **enrichment_summary(units),
            "typography": typography_coverage,
            "renderable": "structural_only",
        },
        "errors": errors,
    }
    return manifest


def extract_document(
    source: str | Path,
    docling_json: dict[str, Any],
    *,
    docling_dir: str | Path | None = None,
    docling_json_key: str | None = None,
    bloom_provider: Any | None = None,
    vision_provider: Any | None = None,
    advisor_provider: Any | None = None,
) -> dict[str, Any]:
    """Full orchestrator — the production entry point.

    Runs the complete pipeline: structural manifest → image captioning →
    Bloom taxonomy → courseware advice. Returns a complete, schema-valid
    manifest (has `taxonomy`, `coursewareAdvice`, `usage`). Providers default
    to the environment-selected ones (deterministic unless
    `CAPTIFY_DEEP_DOC_*_PROVIDER=bedrock`).

    AUDIT F3: `build_manifest()` alone is a structural intermediate; this is
    the function callers should use to obtain a final manifest.
    """
    import time

    from .bloom_layer import apply_bloom  # local import — avoids load-time cycle
    from .courseware_advisor import advise
    from .courseware_advisor import provider_from_environment as advisor_from_env
    from .image_captioner import caption_assets
    from .image_captioner import provider_from_environment as vision_from_env
    from .semantics import provider_from_environment as bloom_from_env
    from .usage_accounting import StageUsage, budget_from_environment, build_usage_block

    # Resolve providers here so their per-stage usage ledgers are reachable
    # afterward (AUDIT F8). If a caller passes None we must not let the inner
    # function create an instance we never see.
    vision_provider = vision_provider or vision_from_env()
    bloom_provider = bloom_provider or bloom_from_env()
    advisor_provider = advisor_provider or advisor_from_env()

    t0 = time.perf_counter()
    manifest = build_manifest(
        source, docling_json, docling_dir=docling_dir, docling_json_key=docling_json_key
    )
    t1 = time.perf_counter()
    caption_coverage = caption_assets(manifest, vision_provider)
    t2 = time.perf_counter()
    apply_bloom(manifest, bloom_provider)
    t3 = time.perf_counter()
    advise(manifest, advisor_provider)
    t4 = time.perf_counter()

    # AUDIT (performance): per-stage timings so a web layer can decide when to
    # go async. Docling conversion time is added by the runner (it owns that
    # subprocess); these cover the in-process assembly.
    manifest["diagnostics"]["stageSeconds"] = {
        "assembly": round(t1 - t0, 3),
        "captioning": round(t2 - t1, 3),
        "bloom": round(t3 - t2, 3),
        "advisor": round(t4 - t3, 3),
    }
    # AUDIT F3: image-caption coverage. A picture asset is "captioned" only
    # when a vision provider produced real caption text; deterministic /
    # missing-file placeholders count as uncaptioned. `imageUnderstanding`
    # is `complete` only when every picture asset carries a real caption.
    picture_assets = caption_coverage.get("pictureAssets", 0)
    captioned = caption_coverage.get("captioned", 0)
    uncaptioned = picture_assets - captioned
    if picture_assets == 0:
        image_understanding = "not_applicable"
    elif uncaptioned == 0:
        image_understanding = "complete"
    else:
        image_understanding = "incomplete"
    manifest["diagnostics"]["imageCaptions"] = {
        "pictureAssets": picture_assets,
        "captionedPictureAssets": captioned,
        "uncaptionedPictureAssets": uncaptioned,
        "captionProvider": caption_coverage.get("provider"),
        "imageUnderstanding": image_understanding,
    }
    # AUDIT F4: measured layout health (overlap / clipping / blank area).
    from .layout_diagnostics import analyze_manifest_layout

    manifest["diagnostics"]["layoutDiagnostics"] = analyze_manifest_layout(manifest)
    # AUDIT F6: record the resolved providers in effectiveOptions.
    manifest["extraction"]["effectiveOptions"]["bloomProvider"] = (
        manifest.get("taxonomy", {}).get("provider")
    )
    caption_methods = {
        (asset.get("caption") or {}).get("provider")
        for asset in manifest.get("assets", [])
        if asset.get("kind") == "picture"
    }
    manifest["extraction"]["effectiveOptions"]["visionProvider"] = (
        sorted(p for p in caption_methods if p) or None
    )

    # AUDIT F8: aggregate per-stage LLM usage into manifest['usage'].
    stages: list[StageUsage] = []
    for provider, stage_name in (
        (vision_provider, "visionCaptioning"),
        (bloom_provider, "bloomClassification"),
        (advisor_provider, "advisor"),
    ):
        stages.append(
            getattr(provider, "usage", None)
            or StageUsage(
                stage=stage_name,
                provider=getattr(provider, "provider_id", "deterministic_fallback"),
            )
        )
    # AUDIT F3: budget ceilings come from the environment, not a constant.
    max_calls, max_tokens = budget_from_environment()
    manifest["usage"] = build_usage_block(
        stages,
        unit_count=len(manifest.get("units", [])),
        size_bytes=manifest.get("source", {}).get("sizeBytes", 0),
        max_llm_calls=max_calls,
        max_total_tokens=max_tokens,
    )
    return manifest
