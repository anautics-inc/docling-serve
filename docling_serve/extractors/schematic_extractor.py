"""Model-driven schematic / engineering-drawing extractor.

Schematics are vector line art: wires, symbols, and labels with custom font
encodings. Generic document extraction (text + tables + raster layout) misses
the connectivity entirely, and no fixed set of symbol templates generalises
across drawing standards. So this extractor *delegates understanding to a
vision model* (Amazon Bedrock) and only orchestrates around it:

1. Export clean vector geometry to SVG (``pdftocairo -svg``) so the drawing can
   be re-opened losslessly in other tools.
2. Replay that geometry — every line, shape, and text outline — into a KiCad
   schematic (``.kicad_sch``) so the drawing opens directly in KiCad (pure
   deterministic conversion, see :mod:`kicad_sch`).
3. Rasterise each page (``pypdfium2``) for the model.
4. Ask the model to read the drawing and return components, pins, nets, and the
   title block as strict JSON — no hard-coded symbol rules in Python.
5. Normalise that into the ``captify.schematic.v1`` graph and a KiCad-style
   ``.net`` netlist (a CAD/EDA interchange format).

When Bedrock is disabled or unreachable the extractor still emits the SVG +
raster (the drawing remains openable) and records a warning, rather than
failing the job.
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
from pathlib import Path
from typing import Any

from docling_serve.deep_document.artifact_writer import write_json
from docling_serve.deep_document.schema_validation import validate_artifact
from docling_serve.extractors.base import (
    ExtractionContext,
    Extractor,
    ExtractorResult,
)
from docling_serve.extractors.docling_extractor import build_docling_structured
from docling_serve.extractors.kicad_sch import (
    KicadConversionError,
    stroked_line_geometry,
    svg_to_kicad_sch,
)
from docling_serve.extractors.net_trace import ComponentBox, TracedNet, trace_nets
from docling_serve.extractors.netlist import graph_to_kicad_netlist
from docling_serve.providers import BedrockUnavailableError, get_bedrock_provider
from docling_serve.settings import docling_serve_settings

_log = logging.getLogger(__name__)

SCHEMATIC_PROFILES = {"schematic", "schematics", "drawing", "drawings"}
SCHEMATIC_SUFFIXES = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".svg"}

SYSTEM_PROMPT = (
    "You are a senior electrical and systems engineer who reads engineering "
    "schematics, wiring diagrams, P&IDs, and mechanical drawings. You identify "
    "every component and trace every connection from the drawing itself. You do "
    "not assume a fixed symbol library; you reason about what each symbol and "
    "label means in context. You always answer with a single valid JSON object "
    "and nothing else."
)

USER_PROMPT = (
    "Analyze this engineering drawing page and extract its structure as JSON.\n"
    "Read the actual drawing — symbols, wires/lines, junctions, labels, and the "
    "title block. Do not invent components that are not present.\n\n"
    "Return ONLY a JSON object with this shape:\n"
    "{\n"
    '  "imageSize": {"w": int, "h": int},\n'
    '  "titleBlock": {"title": str|null, "drawingNumber": str|null, '
    '"revision": str|null, "sheet": str|null, "date": str|null, '
    '"author": str|null, "notes": [str]},\n'
    '  "components": [{"refDes": str|null, "type": str|null, "value": str|null, '
    '"partNumber": str|null, "location": str|null, "parentComponent": str|null, '
    '"description": str|null, "confidence": number|null, "bbox": [x0, y0, x1, y1], '
    '"pins": [{"number": str|null, "name": str|null, '
    '"status": "connected"|"nc"|"spare"|null}]}],\n'
    '  "nets": [{"name": str|null, "class": str|null, "wireId": str|null, '
    '"gauge": str|null, "signalType": str|null, '
    '"nodes": [{"refDes": str, "pin": str|null}]}],\n'
    '  "groundPoints": [{"id": str|null, "name": str|null, "location": str|null}],\n'
    '  "labels": [str],\n'
    '  "confidence": number,\n'
    '  "warnings": [str]\n'
    "}\n\n"
    "Rules:\n"
    '- "imageSize" is the pixel size of THIS image as you measure it; every '
    '"bbox" is in those same pixel coordinates and tightly encloses the '
    "component's symbol graphic together with its refDes/value text. Include a "
    "bbox for every component, even repeated identical ones.\n"
    "- Use the reference designators printed on the drawing (e.g. R1, C3, U2, "
    "K1, J4, TB1). If a component has none, set refDes to null and give a "
    "descriptive type.\n"
    '- "partNumber" and "location" are taken from the drawing only (e.g. '
    '"KIDDE 870929", "RH SIDE, GUN BAY WING, STA 21"); null when not printed.\n'
    '- Connectors (P1, J2, TB1) are components with type "Connector": set '
    '"parentComponent" to the owning component\'s refDes and list EVERY '
    'pin/cavity, marking unused ones with status "nc" or "spare".\n'
    "- A net groups every pin that is electrically/physically connected by a "
    "wire or line. Name nets from the drawing where labeled (e.g. GND, +28V, "
    'SIGNAL_A); set "wireId", "gauge", and "signalType" (power|ground|signal|'
    "data|control) when the drawing prints them; otherwise null.\n"
    "- List every ground stud/eyelet/symbol under groundPoints.\n"
    "- Every node's refDes must match a component you listed (or its description "
    "when refDes is null).\n"
    '- Per-component "confidence" (0-1) reflects how certain you are of that '
    "component's identity; use the top-level confidence for the whole page.\n"
    "- If something is illegible or ambiguous, add a short string to warnings "
    "rather than guessing."
)

# Dedicated detection pass. The main pass reads the whole drawing and its
# component list varies run to run; any symbol it fails to box leaves artwork
# in the line work that can merge adjacent nets during geometric tracing. This
# second, single-purpose pass exists only to box every symbol — its boxes are
# merged with the main pass (by refDes, then by overlap) before tracing.
DETECT_SYSTEM_PROMPT = (
    "You are a precise visual detector for engineering-schematic symbols. You "
    "locate every component symbol on a drawing without exception. You always "
    "answer with a single valid JSON object and nothing else."
)

DETECT_USER_PROMPT = (
    "Locate EVERY component symbol on this engineering drawing page: resistors, "
    "capacitors, inductors, diodes, transistors and transistor arrays, ICs, "
    "switches, buttons, relays, connectors, terminal blocks, displays, tubes, "
    "buzzers/speakers, power sources, crystals, fuses.\n"
    "Return ONLY a JSON object:\n"
    '{"imageSize": {"w": int, "h": int}, "components": [{"refDes": str|null, '
    '"type": str, "bbox": [x0, y0, x1, y1]}]}\n'
    "Rules:\n"
    '- "imageSize" is the pixel size of THIS image as you measure it; every '
    '"bbox" is in those pixel coordinates and tightly encloses the symbol '
    "graphic together with its refDes/value text.\n"
    "- Include EVERY instance, even dozens of repeated identical symbols. "
    "Completeness matters more than classification accuracy.\n"
    "- Do NOT include wires, junction dots, net labels, ground/power rail "
    "symbols, or the title block."
)

#: Detection boxes overlapping an existing component box by at least this IoU
#: are treated as the same component (duplicate suppression).
_DETECTION_IOU_THRESHOLD = 0.3

# Model-response cache. The vision passes are the only nondeterministic part
# of the pipeline (the same drawing yields slightly different component sets
# run to run); caching responses by content hash makes re-ingesting an
# unchanged drawing fully deterministic AND free of model cost. Keyed on the
# page image bytes + prompts + model id, so any prompt or model change busts
# the cache naturally. Disable with DOCLING_SCHEMATIC_MODEL_CACHE=off.
_MODEL_CACHE_ENV = "DOCLING_SCHEMATIC_MODEL_CACHE"
_MODEL_CACHE_DEFAULT = "~/.cache/docling-schematic-model"


def _model_cache_dir() -> Path | None:
    import os

    raw = (os.environ.get(_MODEL_CACHE_ENV) or _MODEL_CACHE_DEFAULT).strip()
    if raw.lower() in {"off", "0", "false", "disabled"}:
        return None
    path = Path(raw).expanduser()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path


def _cached_understand_json(
    provider: Any,
    *,
    prompt: str,
    system: str,
    png_bytes: bytes,
) -> tuple[dict[str, Any], bool]:
    """``understand_json`` with a content-hash disk cache.

    Returns ``(result, was_cached)``. Cache failures degrade to a live call —
    determinism is best-effort, extraction always proceeds.
    """
    import hashlib
    import json as _json

    cache_dir = _model_cache_dir()
    key_file: Path | None = None
    if cache_dir is not None:
        digest = hashlib.sha256()
        for part in (png_bytes, prompt.encode(), system.encode(), provider.vision_model.encode()):
            digest.update(part)
        key_file = cache_dir / f"{digest.hexdigest()}.json"
        if key_file.is_file():
            try:
                return _json.loads(key_file.read_text()), True
            except (OSError, ValueError):
                pass  # corrupt entry — fall through to a live call

    result = provider.understand_json(prompt=prompt, images=[png_bytes], system=system)
    if key_file is not None:
        try:
            tmp = key_file.with_suffix(".tmp")
            tmp.write_text(_json.dumps(result))
            tmp.replace(key_file)
        except OSError:
            pass
    return result, False


class SchematicExtractor(Extractor):
    name = "extract_schematic"

    def supports(self, ctx: ExtractionContext) -> bool:
        suffix = ctx.source_path.suffix.lower()
        if suffix not in SCHEMATIC_SUFFIXES:
            return False
        profile = (ctx.profile or "default").strip().lower()
        if profile in SCHEMATIC_PROFILES:
            return True
        if profile == "auto" and suffix == ".pdf":
            return _looks_like_vector_drawing(ctx.resolve_source_file())
        return False

    def build(self, ctx: ExtractionContext) -> ExtractorResult:
        source_file = ctx.resolve_source_file()
        schematic_dir = ctx.bundle_dir / "schematic"
        schematic_dir.mkdir(parents=True, exist_ok=True)

        # Structural base so the bundle still carries any extractable text.
        structured = self._structural_base(ctx)

        notes: list[str] = []
        warnings: list[str] = []

        max_pages = int(getattr(docling_serve_settings, "bedrock_max_pages", 8))
        dpi = int(getattr(docling_serve_settings, "bedrock_render_dpi", 200))

        ctx.report_progress("schematic_geometry")
        svg_paths = _render_svgs(source_file, schematic_dir, max_pages=max_pages)
        if not svg_paths:
            notes.append("svg_export_unavailable")

        ctx.report_progress("schematic_kicad", pages=len(svg_paths))
        kicad_sch_paths = _export_kicad_sch(
            svg_paths, title=ctx.source_path.stem, warnings=warnings
        )
        if svg_paths and not kicad_sch_paths:
            notes.append("kicad_sch_export_unavailable")

        page_images = _render_page_pngs(source_file, dpi=dpi, max_pages=max_pages)
        for page_no, png_bytes in page_images:
            (ctx.media_dir).mkdir(parents=True, exist_ok=True)
            (ctx.media_dir / f"schematic-page-{page_no:03d}.png").write_bytes(png_bytes)

        provider = get_bedrock_provider()
        model_understood = False
        page_results: list[dict[str, Any]] = []
        if provider.enabled and page_images:
            for page_no, png_bytes in page_images:
                ctx.report_progress("schematic_model_read", page=page_no, pages=len(page_images))
                try:
                    result, cached = _cached_understand_json(
                        provider,
                        prompt=USER_PROMPT,
                        system=SYSTEM_PROMPT,
                        png_bytes=png_bytes,
                    )
                    result["__page__"] = page_no
                    page_results.append(result)
                    model_understood = True
                    if cached:
                        notes.append(f"page {page_no}: model pass served from cache")
                except BedrockUnavailableError as err:
                    _log.warning("Schematic model pass failed on page %s: %s", page_no, err)
                    warnings.append(f"page {page_no}: model pass failed ({err})")
                    continue
                # Second, detection-only pass: box EVERY symbol so unboxed
                # artwork can't merge adjacent nets during geometric tracing.
                # Best-effort — the main pass alone still traces.
                ctx.report_progress(
                    "schematic_model_detect",
                    page=page_no,
                    components=len(result.get("components") or []),
                )
                try:
                    result["__detection__"], _ = _cached_understand_json(
                        provider,
                        prompt=DETECT_USER_PROMPT,
                        system=DETECT_SYSTEM_PROMPT,
                        png_bytes=png_bytes,
                    )
                except BedrockUnavailableError as err:
                    _log.warning("Schematic detection pass failed on page %s: %s", page_no, err)
                    warnings.append(f"page {page_no}: detection pass failed ({err})")
        elif not provider.enabled:
            notes.append("bedrock_disabled_geometry_only")
        elif not page_images:
            warnings.append("no_renderable_pages")

        # Deterministic connectivity: trace wires in the vector geometry, cut
        # at the model-located component boxes. The model names components;
        # the drawing itself defines what connects to what.
        ctx.report_progress("schematic_trace_nets")
        traced_nets_by_page = _trace_all_pages(page_results, svg_paths, warnings)

        ctx.report_progress(
            "schematic_assemble_graph",
            nets=sum(len(nets) for nets in traced_nets_by_page.values()),
        )
        graph = _normalize_graph(
            page_results,
            source_name=ctx.source_path.name,
            model_id=provider.vision_model,
            understood=model_understood,
            svg_paths=[p.relative_to(ctx.bundle_dir).as_posix() for p in svg_paths],
            page_images=page_images,
            warnings=warnings,
            traced_nets_by_page=traced_nets_by_page,
        )
        validate_artifact(graph, "schematic-graph.schema.json")

        graph_path = schematic_dir / "schematic-graph.json"
        write_json(graph_path, graph)
        artifacts = [graph_path.relative_to(ctx.bundle_dir).as_posix()]
        artifacts.extend(p.relative_to(ctx.bundle_dir).as_posix() for p in svg_paths)
        artifacts.extend(p.relative_to(ctx.bundle_dir).as_posix() for p in kicad_sch_paths)

        netlist_text = graph_to_kicad_netlist(graph, source_name=ctx.source_path.name)
        netlist_path = schematic_dir / f"{ctx.source_path.stem}.net"
        netlist_path.write_text(netlist_text)
        artifacts.append(netlist_path.relative_to(ctx.bundle_dir).as_posix())

        # Surface a compact summary on the deep document for UIs.
        structured["schematic"] = {
            "graph": graph_path.relative_to(ctx.bundle_dir).as_posix(),
            "netlist": netlist_path.relative_to(ctx.bundle_dir).as_posix(),
            "svg": [p.relative_to(ctx.bundle_dir).as_posix() for p in svg_paths],
            "kicadSch": [
                p.relative_to(ctx.bundle_dir).as_posix() for p in kicad_sch_paths
            ],
            "componentCount": len(graph["components"]),
            "netCount": len(graph["nets"]),
            "modelUnderstood": model_understood,
        }

        return ExtractorResult(
            structured=structured,
            extractor=self.name,
            domain="schematic",
            artifacts=artifacts,
            manifest_extra={
                "schematic": {
                    "graph": graph_path.relative_to(ctx.bundle_dir).as_posix(),
                    "netlist": netlist_path.relative_to(ctx.bundle_dir).as_posix(),
                    "svg": [p.relative_to(ctx.bundle_dir).as_posix() for p in svg_paths],
                    "kicadSch": [
                        p.relative_to(ctx.bundle_dir).as_posix()
                        for p in kicad_sch_paths
                    ],
                    "model": {
                        "provider": "bedrock",
                        "modelId": provider.vision_model,
                        "understood": model_understood,
                    },
                    "componentCount": len(graph["components"]),
                    "netCount": len(graph["nets"]),
                }
            },
            notes=notes + warnings,
        )

    def _structural_base(self, ctx: ExtractionContext) -> dict[str, Any]:
        if ctx.conv_res is not None and ctx.conv_res.document is not None:
            try:
                return build_docling_structured(ctx)
            except Exception:
                _log.warning(
                    "Docling structural base failed for schematic %s; "
                    "emitting minimal document",
                    ctx.source_path,
                    exc_info=True,
                )
        return _minimal_structured(ctx)


def _minimal_structured(ctx: ExtractionContext) -> dict[str, Any]:
    from datetime import UTC, datetime

    return {
        "schemaVersion": "1.0",
        "artifactKind": "deep_document",
        "documentId": "doc-schematic",
        "sourceManifestKey": ctx.source_manifest_key,
        "createdAt": datetime.now(UTC).isoformat(),
        "source": {"originalFileName": ctx.source_path.name, "fileKind": "schematic"},
        "storage": {"layout": "relative_object_tree", "manifestPath": "deep-document.json"},
        "document": {
            "title": ctx.source_path.stem,
            "unitCount": 0,
            "unitType": "schematic",
            "units": [],
        },
        "assets": [],
        "canvas": {"provider": "tldraw", "shapeMap": {}},
        "rawArtifacts": {},
        "provenance": {"generator": "docling_serve.extractors.schematic_extractor"},
        "errors": [],
    }


def _render_svgs(pdf_path: Path, out_dir: Path, *, max_pages: int) -> list[Path]:
    """Export each page to SVG with poppler's ``pdftocairo``.

    Only PDFs are vectorised; raster sources keep their original image as the
    geometry reference. Returns the written SVG paths (empty if pdftocairo or
    the source is unavailable).
    """
    if pdf_path.suffix.lower() != ".pdf" or not pdf_path.is_file():
        return []
    page_count = _page_count(pdf_path)
    if page_count == 0:
        return []
    written: list[Path] = []
    for page_no in range(1, min(page_count, max_pages) + 1):
        target = out_dir / (
            "schematic.svg" if page_count == 1 else f"schematic-page-{page_no:03d}.svg"
        )
        try:
            subprocess.run(
                [
                    "pdftocairo",
                    "-svg",
                    "-f",
                    str(page_no),
                    "-l",
                    str(page_no),
                    str(pdf_path),
                    str(target),
                ],
                check=True,
                capture_output=True,
                timeout=120,
            )
            if target.is_file():
                written.append(target)
        except FileNotFoundError as err:
            # pdftocairo is not installed — no point trying further pages.
            _log.warning("pdftocairo unavailable for %s: %s", pdf_path, err)
            break
        except subprocess.SubprocessError as err:
            # One bad page must not drop geometry for the rest of the drawing set.
            _log.warning("pdftocairo SVG export failed for %s p%s: %s", pdf_path, page_no, err)
            continue
    return written


def _trace_all_pages(
    page_results: list[dict[str, Any]],
    svg_paths: list[Path],
    warnings: list[str],
) -> dict[int, list[TracedNet]]:
    """Geometric net tracing for every model-understood page with an SVG."""
    svg_by_page: dict[int, Path] = {}
    for svg_path in svg_paths:
        match = re.search(r"page-(\d+)", svg_path.name)
        svg_by_page[int(match.group(1)) if match else 1] = svg_path

    traced: dict[int, list[TracedNet]] = {}
    for result in page_results:
        page_no = int(result.get("__page__") or 1)
        svg_path = svg_by_page.get(page_no)
        if svg_path is None:
            continue
        try:
            nets = _trace_page_nets(svg_path.read_text(), result)
        except Exception as err:  # geometry tracing must never fail the job
            _log.warning("Net tracing failed for page %s: %s", page_no, err, exc_info=True)
            warnings.append(f"page {page_no}: geometric net tracing failed ({err})")
            continue
        if nets is not None:
            traced[page_no] = nets
    return traced


def _scale_bboxes_to_pt(
    result: dict[str, Any], page_w: float, page_h: float
) -> bool:
    """Scale a model result's component bboxes into page pt, in place.

    Bboxes are scaled from the model's self-reported ``imageSize`` pixel space
    straight to page pt — both cover the full page, so the ratio of dimensions
    is the scale regardless of the DPI the raster was rendered at. Scaled
    boxes are stored on each component as ``bboxPt``. Returns False when the
    result has no usable ``imageSize``.
    """
    image_size = result.get("imageSize") or {}
    try:
        model_w = float(image_size.get("w") or 0)
        model_h = float(image_size.get("h") or 0)
    except (TypeError, ValueError):
        return False
    if model_w <= 0 or model_h <= 0:
        return False
    sx, sy = page_w / model_w, page_h / model_h
    for component in result.get("components") or []:
        if not isinstance(component, dict):
            continue
        bbox = component.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) == 4):
            continue
        try:
            x0, y0, x1, y1 = (float(v) for v in bbox)
        except (TypeError, ValueError):
            continue
        component["bboxPt"] = [round(x0 * sx, 2), round(y0 * sy, 2), round(x1 * sx, 2), round(y1 * sy, 2)]
    return True


def _bbox_iou(a: list[float], b: list[float]) -> float:
    """Intersection-over-union of two ``[x0, y0, x1, y1]`` boxes."""
    ix0, iy0 = max(a[0], b[0]), max(a[1], b[1])
    ix1, iy1 = min(a[2], b[2]), min(a[3], b[3])
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    intersection = (ix1 - ix0) * (iy1 - iy0)
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def _merge_detected_components(
    components: list[dict[str, Any]], detected: list[dict[str, Any]]
) -> int:
    """Merge detection-pass boxes (``bboxPt`` set) into the main component list.

    Per detected symbol, in priority order:

    1. Same refDes as a main-pass component → attach the box if it has none.
    2. Box overlaps an existing box (IoU ≥ threshold) → same component, skip.
    3. Otherwise it's a component the main pass missed → append it (typed but
       possibly anonymous), so its artwork is cut out during net tracing.

    Returns the number of components added or given a box.
    """
    by_ref: dict[str, dict[str, Any]] = {}
    for component in components:
        ref = component.get("refDes")
        if ref:
            by_ref[str(ref).strip().upper()] = component

    merged = 0
    anonymous = 0
    for candidate in detected:
        if not isinstance(candidate, dict):
            continue
        bbox_pt = candidate.get("bboxPt")
        if not bbox_pt:
            continue
        ref = candidate.get("refDes")
        ref_key = str(ref).strip().upper() if ref else None

        existing = by_ref.get(ref_key) if ref_key else None
        if existing is not None:
            if not existing.get("bboxPt"):
                existing["bboxPt"] = bbox_pt
                merged += 1
            continue

        if any(
            _bbox_iou(bbox_pt, component["bboxPt"]) >= _DETECTION_IOU_THRESHOLD
            for component in components
            if component.get("bboxPt")
        ):
            continue

        detected_type = _str_or_none(candidate.get("type"))
        if not ref:
            anonymous += 1
        addition = {
            "refDes": _str_or_none(ref),
            "type": detected_type,
            "value": None,
            # The description doubles as the net-node key when refDes is null
            # (same derivation as _normalize_graph) — make it unique so two
            # anonymous symbols of the same type don't collapse into one node.
            "description": (
                None if ref else f"{detected_type or 'component'} (detected #{anonymous})"
            ),
            "pins": [],
            "bboxPt": bbox_pt,
            "detectedOnly": True,
        }
        components.append(addition)
        if ref_key:
            by_ref[ref_key] = addition
        merged += 1
    return merged


def _trace_page_nets(
    svg_text: str, result: dict[str, Any]
) -> list[TracedNet] | None:
    """Trace one page's nets from its SVG line work and model component boxes.

    Boxes come from the main model pass merged with the detection-only pass
    (``__detection__``, see :data:`DETECT_USER_PROMPT`) — every boxed symbol is
    cut out of the line work, so a symbol the main pass missed can no longer
    merge its neighbouring nets. Returns ``None`` when no pass carries usable
    bounding boxes (the caller then falls back to the model's own nets).
    Component refs use the same key derivation as :func:`_normalize_graph` so
    traced net nodes resolve to component ids.
    """
    (page_w, page_h), polylines = stroked_line_geometry(svg_text)

    components = [c for c in result.get("components") or [] if isinstance(c, dict)]
    _scale_bboxes_to_pt(result, page_w, page_h)

    detection = result.get("__detection__")
    if isinstance(detection, dict) and _scale_bboxes_to_pt(detection, page_w, page_h):
        added = _merge_detected_components(
            components, [c for c in detection.get("components") or [] if isinstance(c, dict)]
        )
        if added:
            _log.info("Detection pass contributed %s component boxes", added)
        # New components must reach the graph; rebuild the result list in case
        # it was not the same object we filtered from.
        result["components"] = components

    boxes: list[ComponentBox] = []
    for index, component in enumerate(components):
        bbox_pt = component.get("bboxPt")
        if not bbox_pt:
            continue
        key = component.get("refDes") or component.get("description") or f"component-{index}"
        boxes.append(ComponentBox(str(key), *(float(v) for v in bbox_pt)))

    if len(boxes) < 2:
        return None
    return trace_nets(polylines, boxes)


def _export_kicad_sch(
    svg_paths: list[Path], *, title: str, warnings: list[str]
) -> list[Path]:
    """Replay each exported SVG into a sibling ``.kicad_sch`` schematic.

    The conversion is deterministic geometry replay (no model). A failed page
    records a warning instead of failing the job — the SVG remains the
    lossless reference.
    """
    written: list[Path] = []
    for svg_path in svg_paths:
        target = svg_path.with_suffix(".kicad_sch")
        try:
            target.write_text(svg_to_kicad_sch(svg_path.read_text(), title=title))
            written.append(target)
        except (KicadConversionError, OSError) as err:
            _log.warning("KiCad export failed for %s: %s", svg_path, err)
            warnings.append(f"kicad_sch export failed for {svg_path.name} ({err})")
    return written


def _page_count(pdf_path: Path) -> int:
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover - pypdfium2 is a docling dep
        return 0
    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            return len(pdf)
        finally:
            pdf.close()
    except Exception:  # pragma: no cover - environment dependent
        return 0


def _render_page_pngs(
    source_path: Path, *, dpi: int, max_pages: int
) -> list[tuple[int, bytes]]:
    """Rasterise pages to PNG bytes for the vision model.

    PDFs render via pypdfium2; a raster source is read as a single page.
    """
    suffix = source_path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
        try:
            return [(1, _normalise_raster(source_path.read_bytes()))]
        except Exception as err:  # pragma: no cover
            _log.warning("Failed to read raster schematic %s: %s", source_path, err)
            return []
    if suffix != ".pdf":
        return []
    try:
        import pypdfium2 as pdfium
    except ImportError:  # pragma: no cover
        return []
    images: list[tuple[int, bytes]] = []
    try:
        pdf = pdfium.PdfDocument(str(source_path))
    except Exception as err:  # pragma: no cover
        _log.warning("pypdfium2 could not open %s: %s", source_path, err)
        return []
    try:
        scale = max(0.5, dpi / 72.0)
        for index in range(min(len(pdf), max_pages)):
            try:
                page = pdf[index]
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil()
                images.append((index + 1, _encode_png(pil_image)))
            except Exception as err:  # pragma: no cover
                _log.warning("Render failed for %s page %s: %s", source_path, index + 1, err)
    finally:
        pdf.close()
    return images


def _encode_png(pil_image: Any, *, max_bytes: int = 4_000_000) -> bytes:
    """Encode a PIL image to PNG, down-scaling until under ``max_bytes``."""
    image = pil_image
    for _ in range(4):
        buffer = io.BytesIO()
        rgb = image.convert("RGB") if image.mode not in {"RGB", "L"} else image
        rgb.save(buffer, format="PNG", optimize=True)
        data = buffer.getvalue()
        if len(data) <= max_bytes:
            return data
        width, height = image.size
        image = image.resize((max(1, width * 3 // 4), max(1, height * 3 // 4)))
    return data


def _normalise_raster(raw: bytes, *, max_bytes: int = 4_000_000) -> bytes:
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return raw
    image = Image.open(io.BytesIO(raw))
    return _encode_png(image, max_bytes=max_bytes)


def _looks_like_vector_drawing(pdf_path: Path) -> bool:
    """Heuristic router for ``profile=auto``: vector-heavy PDF with no images.

    Used only to *route* to this extractor; the model still does the
    understanding. Conservative: returns False on any uncertainty.
    """
    try:
        import re
        import zlib

        data = pdf_path.read_bytes()
    except Exception:  # pragma: no cover
        return False
    if b"/Subtype /Image" in data or b"/Subtype/Image" in data:
        return False
    streams = re.findall(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S)
    path_ops = 0
    for stream in streams[:200]:
        try:
            decoded = zlib.decompress(stream)
        except Exception:
            continue
        for op in (rb"(?<![A-Za-z])l(?![A-Za-z])", rb"(?<![A-Za-z])c(?![A-Za-z])"):
            path_ops += len(re.findall(op, decoded))
        if path_ops > 200:
            return True
    return path_ops > 200


def _normalize_graph(
    page_results: list[dict[str, Any]],
    *,
    source_name: str,
    model_id: str,
    understood: bool,
    svg_paths: list[str],
    page_images: list[tuple[int, bytes]],
    warnings: list[str],
    traced_nets_by_page: dict[int, list[TracedNet]] | None = None,
) -> dict[str, Any]:
    """Merge per-page model output into a single validated schematic graph.

    Assigns stable component/pin/net ids and rewrites net node references to
    those ids. Cross-page identity is resolved here:

    * A **refDes is global** to the drawing set (standard EE practice), so the
      same refDes appearing on multiple sheets merges into ONE component (pins
      unioned, ``pages`` records every sheet it appears on). Components without
      a refDes stay page-scoped (a bare "relay" on page 2 is not assumed to be
      page 1's "relay").
    * **Named nets merge across pages** (GND / +28V / off-page connectors carry
      their label); unnamed nets stay per-page.

    When a page has geometrically traced nets (``traced_nets_by_page``) those
    replace the model's nets for that page — the drawing's line work is the
    authority on connectivity — with names recovered from the best-overlapping
    model net. Power nets split across separate symbol instances (one GND
    symbol per branch) reunite in :func:`_merge_named_nets` via that name.

    The model's extra fields are preserved (schema is open).
    """
    components: list[dict[str, Any]] = []
    nets: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    ground_points: list[dict[str, Any]] = []
    all_warnings = list(warnings)
    confidences: list[float] = []

    # Component identity: refDes is global; description-keyed fallback is per-page.
    ref_to_id: dict[str, str] = {}
    page_key_to_id: dict[tuple[int, str], str] = {}
    comp_by_id: dict[str, dict[str, Any]] = {}
    comp_counter = 0

    raster_by_page = {page_no: f"media/schematic-page-{page_no:03d}.png" for page_no, _ in page_images}

    for result in page_results:
        page_no = int(result.get("__page__") or 1)
        title_block = result.get("titleBlock") if isinstance(result.get("titleBlock"), dict) else {}
        pages.append(
            {
                "pageNumber": page_no,
                "svg": next((s for s in svg_paths if f"page-{page_no:03d}" in s or s.endswith("schematic.svg")), None),
                "raster": raster_by_page.get(page_no),
                "titleBlock": title_block or {},
            }
        )
        if isinstance(result.get("confidence"), (int, float)):
            confidences.append(float(result["confidence"]))
        for warning in result.get("warnings") or []:
            all_warnings.append(f"page {page_no}: {warning}")

        for component in result.get("components") or []:
            if not isinstance(component, dict):
                continue
            comp_counter = _ingest_component(
                component,
                page_no,
                comp_counter,
                components=components,
                comp_by_id=comp_by_id,
                ref_to_id=ref_to_id,
                page_key_to_id=page_key_to_id,
            )

        for ground_index, ground in enumerate(result.get("groundPoints") or [], start=1):
            if not isinstance(ground, dict):
                continue
            ground_points.append(
                {
                    "id": _str_or_none(ground.get("id")) or f"GND{page_no:03d}-{ground_index:02d}",
                    "name": _str_or_none(ground.get("name")),
                    "location": _str_or_none(ground.get("location")),
                    "page": page_no,
                }
            )

        model_nets = [n for n in result.get("nets") or [] if isinstance(n, dict)]
        traced = (traced_nets_by_page or {}).get(page_no)
        if traced:
            nets.extend(
                _traced_to_graph_nets(
                    traced, model_nets, page_no, ref_to_id, page_key_to_id
                )
            )
        else:
            nets.extend(
                _model_nets_to_graph_nets(
                    model_nets, page_no, ref_to_id, page_key_to_id
                )
            )

    nets = _merge_named_nets(nets)

    if not pages:
        pages = [
            {"pageNumber": page_no, "svg": None, "raster": raster, "titleBlock": {}}
            for page_no, raster in raster_by_page.items()
        ]

    return {
        "schemaVersion": "1.0",
        "artifactKind": "captify.schematic.v1",
        "source": {"originalFileName": source_name},
        "model": {"provider": "bedrock", "modelId": model_id, "understood": understood},
        "pages": pages,
        "components": components,
        "nets": nets,
        "groundPoints": ground_points,
        "confidence": (sum(confidences) / len(confidences)) if confidences else None,
        "warnings": all_warnings,
        "notes": [] if understood else ["geometry exported; model understanding unavailable"],
    }


def _ingest_component(
    component: dict[str, Any],
    page_no: int,
    comp_counter: int,
    *,
    components: list[dict[str, Any]],
    comp_by_id: dict[str, dict[str, Any]],
    ref_to_id: dict[str, str],
    page_key_to_id: dict[tuple[int, str], str],
) -> int:
    """Register one model component sighting; returns the updated counter.

    A refDes seen on an earlier sheet merges into that component (pins
    unioned, blanks filled); otherwise a new component record is created.
    """
    ref_des = _str_or_none(component.get("refDes"))
    ref_key = ref_des.upper() if ref_des else None

    existing = comp_by_id.get(ref_to_id[ref_key]) if ref_key in ref_to_id else None
    if existing is not None:
        # Same refDes on another sheet: ONE component. Union pins; keep
        # first-seen scalar fields, fill blanks from the new sighting.
        comp_id = existing["id"]
        if page_no not in existing["pages"]:
            existing["pages"].append(page_no)
        known_pins = {(pin.get("number"), pin.get("name")) for pin in existing["pins"]}
        for pin in component.get("pins") or []:
            if not isinstance(pin, dict):
                continue
            pin_key = (_str_or_none(pin.get("number")), _str_or_none(pin.get("name")))
            if pin_key in known_pins:
                continue
            known_pins.add(pin_key)
            existing["pins"].append(
                {
                    "id": f"{comp_id}-p{len(existing['pins']) + 1}",
                    "number": pin_key[0],
                    "name": pin_key[1],
                    "status": _str_or_none(pin.get("status")),
                    "net": None,
                }
            )
        for field in ("type", "value", "description", "partNumber", "location", "parentComponent"):
            if not existing.get(field):
                existing[field] = _str_or_none(component.get(field))
        page_key_to_id[(page_no, ref_des)] = comp_id
        return comp_counter

    comp_counter += 1
    comp_id = f"C{comp_counter:04d}"
    key = str(ref_des or component.get("description") or comp_id)
    if ref_key:
        ref_to_id[ref_key] = comp_id
    page_key_to_id[(page_no, key)] = comp_id
    pins = []
    for pin_index, pin in enumerate(component.get("pins") or [], start=1):
        if not isinstance(pin, dict):
            continue
        pins.append(
            {
                "id": f"{comp_id}-p{pin_index}",
                "number": _str_or_none(pin.get("number")),
                "name": _str_or_none(pin.get("name")),
                "status": _str_or_none(pin.get("status")),
                "net": None,
            }
        )
    record = {
        "id": comp_id,
        "refDes": ref_des,
        "type": _str_or_none(component.get("type")),
        "value": _str_or_none(component.get("value")),
        # Drawing-printed part identity + install location: the join keys for
        # matching this symbol to ontology part/assembly entities.
        "partNumber": _str_or_none(component.get("partNumber")),
        "location": _str_or_none(component.get("location")),
        # Connectors carry the refDes of the component they belong to.
        "parentComponent": _str_or_none(component.get("parentComponent")),
        "description": _str_or_none(component.get("description")),
        "confidence": (
            float(component["confidence"])
            if isinstance(component.get("confidence"), (int, float))
            else None
        ),
        "page": page_no,
        "pages": [page_no],
        # Page-pt bounding box (set during net tracing) for viewers.
        "bbox": component.get("bboxPt"),
        "pins": pins,
    }
    comp_by_id[comp_id] = record
    components.append(record)
    return comp_counter


def _model_nets_to_graph_nets(
    model_nets: list[dict[str, Any]],
    page_no: int,
    ref_to_id: dict[str, str],
    page_key_to_id: dict[tuple[int, str], str],
) -> list[dict[str, Any]]:
    """Convert the model's own net list into graph nets (no tracing fallback)."""
    graph_nets: list[dict[str, Any]] = []
    for net_index, net in enumerate(model_nets, start=1):
        nodes = []
        for node in net.get("nodes") or []:
            if not isinstance(node, dict):
                continue
            ref = node.get("refDes")
            if ref is None:
                continue
            comp_id = (
                ref_to_id.get(str(ref).upper())
                or page_key_to_id.get((page_no, str(ref)))
            )
            nodes.append(
                {
                    "component": comp_id or str(ref),
                    "pin": _str_or_none(node.get("pin")),
                }
            )
        if nodes:
            graph_nets.append(
                {
                    "id": f"N{page_no:03d}-{net_index:04d}",
                    "name": _str_or_none(net.get("name")),
                    "class": _str_or_none(net.get("class")),
                    "wireId": _str_or_none(net.get("wireId")),
                    "gauge": _str_or_none(net.get("gauge")),
                    "signalType": _str_or_none(net.get("signalType")),
                    "nodes": nodes,
                }
            )
    return graph_nets


#: A traced net adopts a model net's name when at least this share of its
#: components appear in that model net (and they share ≥ 2 components).
_NET_NAME_MATCH_RATIO = 0.6


def _traced_to_graph_nets(
    traced: list[TracedNet],
    model_nets: list[dict[str, Any]],
    page_no: int,
    ref_to_id: dict[str, str],
    page_key_to_id: dict[tuple[int, str], str],
) -> list[dict[str, Any]]:
    """Convert geometrically traced nets into graph nets, naming from the model.

    Geometry gives reliable membership but no labels; the model's nets give
    labels but unreliable membership. Each traced net takes the name/class and
    wire metadata (printed wire id, gauge, signal type) of the model net
    containing most of its components. Several traced clusters may
    legitimately adopt the same power-net name (one symbol per branch);
    ``_merge_named_nets`` unifies them afterwards.
    """
    _LABEL_FIELDS = ("name", "class", "wireId", "gauge", "signalType")
    labeled_model_sets: list[tuple[set[str], dict[str, str | None], dict[str, list[str]]]] = []
    for net in model_nets:
        labels = {field: _str_or_none(net.get(field)) for field in _LABEL_FIELDS}
        refs: set[str] = set()
        # The model's pin claims per component — geometry can't see pin
        # numbers, so a matched traced net adopts them (pinSource: "model").
        # A component may legitimately join a net on several pins.
        pins_by_ref: dict[str, list[str]] = {}
        for node in net.get("nodes") or []:
            if not isinstance(node, dict) or not node.get("refDes"):
                continue
            ref_upper = str(node["refDes"]).upper()
            refs.add(ref_upper)
            pin = _str_or_none(node.get("pin"))
            if pin:
                pins_by_ref.setdefault(ref_upper, []).append(pin)
        if refs and (any(labels.values()) or pins_by_ref):
            labeled_model_sets.append((refs, labels, pins_by_ref))

    graph_nets: list[dict[str, Any]] = []
    for index, traced_net in enumerate(traced, start=1):
        refs_upper = {ref.upper() for ref in traced_net.components}
        best_labels: dict[str, str | None] = dict.fromkeys(_LABEL_FIELDS)
        best_pins: dict[str, list[str]] = {}
        best_score = 0.0
        for model_refs, labels, pins_by_ref in labeled_model_sets:
            shared = len(refs_upper & model_refs)
            if shared < 2:
                continue
            score = shared / len(refs_upper)
            if score > best_score:
                best_score, best_labels, best_pins = score, labels, pins_by_ref
        if best_score < _NET_NAME_MATCH_RATIO:
            best_labels = dict.fromkeys(_LABEL_FIELDS)
            best_pins = {}

        # One node per PHYSICAL connection: the geometric attachment points
        # (where wires meet the component box) define how many times the
        # component joins this net; the model's pin claims are assigned to
        # them in order. A component with no resolvable attachment still gets
        # one membership node.
        nodes = []
        for ref in traced_net.components:
            comp_id = (
                ref_to_id.get(ref.upper()) or page_key_to_id.get((page_no, ref)) or ref
            )
            pins = list(best_pins.get(ref.upper(), []))
            points = traced_net.attachments.get(ref) or [None]
            for slot, point in enumerate(points):
                pin = pins[slot] if slot < len(pins) else None
                node: dict[str, Any] = {"component": comp_id, "pin": pin}
                if pin:
                    node["pinSource"] = "model"
                if point is not None:
                    node["attachment"] = [round(point[0], 1), round(point[1], 1)]
                nodes.append(node)
        graph_nets.append(
            {
                "id": f"N{page_no:03d}-{index:04d}",
                "name": best_labels["name"],
                "class": best_labels["class"],
                "wireId": best_labels["wireId"],
                "gauge": best_labels["gauge"],
                "signalType": best_labels["signalType"],
                "nodes": nodes,
                "source": "geometry",
                "page": page_no,
                # The net's wire pieces in page pt — lets viewers draw the
                # copper itself (click a wire, highlight the whole net).
                # De-duplicated: drawings sometimes stroke the same span twice,
                # and viewers key on the coordinates.
                "segments": [
                    list(segment)
                    for segment in dict.fromkeys(
                        (round(a[0], 1), round(a[1], 1), round(b[0], 1), round(b[1], 1))
                        for a, b in traced_net.segments
                    )
                ],
            }
        )
    return graph_nets


def _merge_named_nets(nets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge nets that share a (case-insensitive) name across pages.

    A labeled net (GND, +28V, SIG_A, off-page connector names) is one electrical
    net no matter how many sheets it spans; its nodes are unioned (de-duplicated
    per component+pin). Unnamed nets are page-local and pass through untouched.
    """
    merged: dict[str, dict[str, Any]] = {}
    out: list[dict[str, Any]] = []
    for net in nets:
        name = net.get("name")
        key = str(name).strip().upper() if name else None
        if not key:
            out.append(net)
            continue
        target = merged.get(key)
        if target is None:
            merged[key] = net
            out.append(net)
            continue
        known = {(n.get("component"), n.get("pin")) for n in target["nodes"]}
        for node in net["nodes"]:
            node_key = (node.get("component"), node.get("pin"))
            if node_key not in known:
                known.add(node_key)
                target["nodes"].append(node)
        if not target.get("class") and net.get("class"):
            target["class"] = net["class"]
    return out


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
