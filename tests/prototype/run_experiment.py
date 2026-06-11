from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
PROTOTYPE = ROOT / "tests" / "prototype"
if str(PROTOTYPE) not in sys.path:
    sys.path.insert(0, str(PROTOTYPE))

from deep_document.ooxml import (  # noqa: E402
    A_NS,
    P_NS,
    MediaAsset,
    _mime_for,
    bbox_for,
    direct_renderable_shapes,
    rel_id_for_picture,
    normalize_part_target,
    parse_pptx,
    read_xml,
    rels_for,
    shape_name,
)
from deep_document.theme import parse_theme  # noqa: E402
from deep_document.image_captioner import provider_from_environment as vision_provider_from_environment  # noqa: E402
from docling_serve.powerpoint_courseware.artifact_writer import write_course_artifacts  # noqa: E402
from docling_serve.powerpoint_courseware import build_course_artifacts  # noqa: E402
from fixture_manifests import manifest_for_file  # noqa: E402


SOURCE = (
    ROOT
    / "tests"
    / "test_files"
    / "1220dd73-5621-458d-950e-657a6738fb14-updated AFTO Form 874 for presentation.pptx"
)
PDF_REFERENCE_SOURCE = SOURCE.with_suffix(".pdf")
MULTI_FORMAT_SOURCES = {
    "docx": ROOT / "tests" / "test_files" / "generated-code-validation-procedures.docx",
    "xlsx": ROOT / "tests" / "test_files" / "generated-training-workbook.xlsx",
    "pdf": ROOT / "tests" / "test_files" / "titan-authorized-user-acceptable-use-policy.pdf",
}
OUT = ROOT / "tests" / "prototype" / "out"
ASSET_DIR = OUT / "assets"
XML_DIR = OUT / "xml"
SLIDE_PNG_DIR = OUT / "slide-png"

EMU_PER_INCH = 914400
PX_PER_INCH = 96
TARGET_SLIDE_WIDTH_PX = 960


def sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


def emu_to_in(value: int | float) -> float:
    return round(float(value) / EMU_PER_INCH, 4)


def normalize_bbox(bbox: dict[str, Any], slide_size: dict[str, int]) -> dict[str, Any]:
    slide_width_emu = max(int(slide_size.get("cx", 0)), 1)
    scale = TARGET_SLIDE_WIDTH_PX / slide_width_emu
    return {
        "emu": {
            "x": int(bbox.get("x", 0)),
            "y": int(bbox.get("y", 0)),
            "w": int(bbox.get("cx", 0)),
            "h": int(bbox.get("cy", 0)),
        },
        "inches": {
            "x": emu_to_in(bbox.get("x", 0)),
            "y": emu_to_in(bbox.get("y", 0)),
            "w": emu_to_in(bbox.get("cx", 0)),
            "h": emu_to_in(bbox.get("cy", 0)),
        },
        "px": {
            "x": round(int(bbox.get("x", 0)) * scale, 2),
            "y": round(int(bbox.get("y", 0)) * scale, 2),
            "w": round(int(bbox.get("cx", 0)) * scale, 2),
            "h": round(int(bbox.get("cy", 0)) * scale, 2),
        },
    }


def normalize_slide_size(slide_size: dict[str, int]) -> dict[str, Any]:
    width = int(slide_size.get("cx", 0))
    height = int(slide_size.get("cy", 0))
    scale = TARGET_SLIDE_WIDTH_PX / width if width else 1
    return {
        "emu": {"w": width, "h": height},
        "inches": {"w": emu_to_in(width), "h": emu_to_in(height)},
        "px": {
            "w": TARGET_SLIDE_WIDTH_PX if width else 0,
            "h": round(height * scale, 2) if height else 0,
        },
    }


def is_zero_bbox(bbox: dict[str, Any]) -> bool:
    return int(bbox.get("cx", 0)) <= 0 or int(bbox.get("cy", 0)) <= 0


def placeholder_ref(shape: Any) -> tuple[str | None, str | None]:
    ph = shape.find(f".//{P_NS}ph")
    if ph is None:
        return (None, None)
    return (ph.attrib.get("type"), ph.attrib.get("idx"))


def norm_placeholder_type(ph_type: str | None) -> str:
    if ph_type in {"title", "ctrTitle"}:
        return "title"
    return ph_type or "body"


def xfrm_rotation_degrees(shape: Any) -> float:
    xfrm = shape.find(f".//{A_NS}xfrm")
    if xfrm is None:
        return 0.0
    return round(int(xfrm.attrib.get("rot", "0")) / 60000, 4)


def color_from_node(node: Any, theme: dict[str, Any] | None) -> str | None:
    if node is None:
        return None
    srgb = node.find(f".//{A_NS}srgbClr")
    if srgb is not None and srgb.attrib.get("val"):
        return f"#{srgb.attrib['val']}"
    scheme = node.find(f".//{A_NS}schemeClr")
    if scheme is not None and scheme.attrib.get("val"):
        return ((theme or {}).get("colorScheme") or {}).get(scheme.attrib["val"])
    return None


def shape_visual_style(shape: Any, theme: dict[str, Any] | None) -> dict[str, str | None]:
    sp_pr = shape.find(f"{P_NS}spPr")
    if sp_pr is None:
        return {"fillColor": None, "lineColor": None}

    fill_color = None
    for tag in ("solidFill", "gradFill"):
        fill = sp_pr.find(f"{A_NS}{tag}")
        if fill is not None:
            fill_color = color_from_node(fill, theme)
            break

    line = sp_pr.find(f"{A_NS}ln")
    line_color = None
    if line is not None and line.find(f"{A_NS}noFill") is None:
        line_color = color_from_node(line, theme)

    return {"fillColor": fill_color, "lineColor": line_color}


def has_visible_shape_paint(shape: Any, theme: dict[str, Any] | None) -> bool:
    style = shape_visual_style(shape, theme)
    return bool(style["fillColor"] or style["lineColor"])


def shape_metadata_by_slide(source: Path) -> dict[str, dict[int, dict[str, Any]]]:
    by_slide: dict[str, dict[int, dict[str, Any]]] = {}
    with zipfile.ZipFile(source) as zf:
        for slide_part in [
            part for part in zf.namelist() if part.startswith("ppt/slides/slide") and part.endswith(".xml")
        ]:
            root = read_xml(zf, slide_part)
            by_slide[slide_part] = {}
            for index, shape in enumerate(direct_renderable_shapes(root), start=1):
                ph_type, ph_idx = placeholder_ref(shape)
                by_slide[slide_part][index] = {
                    "placeholderType": ph_type,
                    "placeholderIndex": ph_idx,
                    "rotationDegrees": xfrm_rotation_degrees(shape),
                }
    return by_slide


def match_placeholder(root: Any, ph_type: str | None, ph_idx: str | None) -> Any | None:
    if root is None:
        return None
    candidates = [
        shape
        for shape in root.findall(f".//{P_NS}sp")
        if shape.find(f".//{P_NS}ph") is not None
    ]
    if ph_idx is not None:
        for shape in candidates:
            _, idx = placeholder_ref(shape)
            if idx == ph_idx:
                return shape
    want = norm_placeholder_type(ph_type)
    for shape in candidates:
        candidate_type, _ = placeholder_ref(shape)
        if norm_placeholder_type(candidate_type) == want:
            return shape
    return None


def inherited_placeholder_bbox(
    zf: zipfile.ZipFile,
    slide_part: str,
    ph_type: str | None,
    ph_idx: str | None,
) -> tuple[dict[str, int] | None, str | None]:
    slide_rels = rels_for(zf, slide_part)
    layout_part = next(
        (
            normalize_part_target(slide_part, rel["target"])
            for rel in slide_rels.values()
            if rel["type"].endswith("/slideLayout")
        ),
        None,
    )
    if not layout_part:
        return (None, None)

    layout_root = read_xml(zf, layout_part)
    layout_shape = match_placeholder(layout_root, ph_type, ph_idx)
    if layout_shape is not None:
        bbox = bbox_for(layout_shape)
        if not is_zero_bbox(bbox):
            return (bbox, "layout_placeholder")

    layout_rels = rels_for(zf, layout_part)
    master_part = next(
        (
            normalize_part_target(layout_part, rel["target"])
            for rel in layout_rels.values()
            if rel["type"].endswith("/slideMaster")
        ),
        None,
    )
    if not master_part:
        return (None, None)
    master_root = read_xml(zf, master_part)
    master_shape = match_placeholder(master_root, ph_type, None)
    if master_shape is None:
        master_shape = match_placeholder(master_root, ph_type, ph_idx)
    if master_shape is not None:
        bbox = bbox_for(master_shape)
        if not is_zero_bbox(bbox):
            return (bbox, "master_placeholder")
    return (None, None)


def resolved_bbox_for_block(
    source: Path,
    slide_part: str,
    block_bbox: dict[str, Any],
    ph_type: str | None,
    ph_idx: str | None,
) -> tuple[dict[str, Any], str]:
    if not is_zero_bbox(block_bbox):
        return (block_bbox, "slide_shape")
    if ph_type is None and ph_idx is None:
        return (block_bbox, "slide_shape_missing_transform")
    with zipfile.ZipFile(source) as zf:
        inherited, source_label = inherited_placeholder_bbox(zf, slide_part, ph_type, ph_idx)
    if inherited is not None and source_label is not None:
        return (inherited, source_label)
    return (block_bbox, "unresolved_placeholder")


def slide_layout_and_master(
    zf: zipfile.ZipFile, slide_part: str
) -> tuple[str | None, str | None]:
    slide_rels = rels_for(zf, slide_part)
    layout_part = next(
        (
            normalize_part_target(slide_part, rel["target"])
            for rel in slide_rels.values()
            if rel["type"].endswith("/slideLayout")
        ),
        None,
    )
    if not layout_part:
        return (None, None)
    layout_rels = rels_for(zf, layout_part)
    master_part = next(
        (
            normalize_part_target(layout_part, rel["target"])
            for rel in layout_rels.values()
            if rel["type"].endswith("/slideMaster")
        ),
        None,
    )
    return (layout_part, master_part)


def register_related_image_asset(
    zf: zipfile.ZipFile,
    source_part: str,
    rel_id: str | None,
    assets: dict[str, MediaAsset],
) -> str | None:
    if not rel_id:
        return None
    rel = rels_for(zf, source_part).get(rel_id)
    if not rel:
        return None
    target = normalize_part_target(source_part, rel["target"])
    if target not in zf.namelist():
        return None
    binary = zf.read(target)
    digest = sha256_bytes(binary)
    asset_id = f"asset-{digest[:8]}"
    asset = assets.get(asset_id)
    if asset is None:
        suffix = Path(target).suffix.lower() or ".bin"
        mime = _mime_for(target)
        asset = MediaAsset(
            asset_id=asset_id,
            kind="image" if mime.startswith("image/") else "binary",
            mime_type=mime,
            size_bytes=len(binary),
            sha256=digest,
            extension=suffix,
            binary=binary,
        )
        assets[asset_id] = asset
    asset.source_parts.add(target)
    return asset_id


def is_decorative_master_shape(shape: Any, theme: dict[str, Any] | None) -> bool:
    if shape.find(f".//{P_NS}ph") is not None:
        return False
    bbox = bbox_for(shape)
    if is_zero_bbox(bbox):
        return False
    if shape.tag == f"{P_NS}pic":
        return True
    if shape.tag == f"{P_NS}sp":
        return has_visible_shape_paint(shape, theme)
    return False


def inherited_decorations_by_slide(
    source: Path,
    slides: list[dict[str, Any]],
    assets: dict[str, MediaAsset],
    theme: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    decorations: dict[str, list[dict[str, Any]]] = {}
    with zipfile.ZipFile(source) as zf:
        for slide in slides:
            slide_part = slide["sourcePart"]
            _layout_part, master_part = slide_layout_and_master(zf, slide_part)
            if not master_part:
                decorations[slide_part] = []
                continue
            master_root = read_xml(zf, master_part)
            inherited: list[dict[str, Any]] = []
            for index, shape in enumerate(direct_renderable_shapes(master_root), start=1):
                if not is_decorative_master_shape(shape, theme):
                    continue
                asset_id = None
                kind = "master_shape"
                visual_style = shape_visual_style(shape, theme)
                if shape.tag == f"{P_NS}pic":
                    kind = "master_image"
                    asset_id = register_related_image_asset(
                        zf, master_part, rel_id_for_picture(shape), assets
                    )
                    if asset_id:
                        assets[asset_id].used_by.append(
                            {
                                "unitId": slide["unitId"],
                                "blockId": f"{slide['unitId']}-master-{len(inherited) + 1:03d}",
                                "relationshipId": rel_id_for_picture(shape) or "",
                            }
                        )
                inherited.append(
                    {
                        "sourcePart": master_part,
                        "shapeIndex": index,
                        "shapeName": shape_name(shape),
                        "kind": kind,
                        "assetId": asset_id,
                        "bbox": bbox_for(shape),
                        "rotationDegrees": xfrm_rotation_degrees(shape),
                        "visualStyle": visual_style,
                    }
                )
            decorations[slide_part] = inherited
    return decorations


def element_type(kind: str) -> str:
    return {
        "text": "text",
        "picture": "image",
        "table": "table",
        "chart_placeholder": "chart",
        "smartart_placeholder": "smartart",
        "group": "group",
    }.get(kind, "shape")


def image_context_for_assets(
    assets: dict[str, MediaAsset],
    target_asset_ids: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    provider = vision_provider_from_environment()
    contexts: dict[str, dict[str, Any]] = {}
    captioned = 0
    placeholder = 0
    skipped = 0
    embedded_ocr_assets = 0
    embedded_ocr_words = 0
    embedded_grid_assets = 0
    for asset in sorted(assets.values(), key=lambda item: item.asset_id):
        if asset.kind != "image":
            continue
        if asset.asset_id not in target_asset_ids:
            continue
        if not asset.binary:
            contexts[asset.asset_id] = {
                "text": None,
                "provider": "deterministic_fallback",
                "method": "image_binary_missing",
                "reason": "Image binary was not available for vision review.",
            }
            skipped += 1
            continue
        context = provider.caption(asset.binary, asset.mime_type)
        embedded_extraction = extract_embedded_image_content(asset)
        if embedded_extraction.get("text"):
            context["embeddedExtraction"] = embedded_extraction
            embedded_ocr_assets += 1
            embedded_ocr_words += embedded_extraction.get("wordCount") or 0
            if (embedded_extraction.get("grid") or {}).get("tableLike"):
                embedded_grid_assets += 1
        contexts[asset.asset_id] = context
        if context.get("text"):
            captioned += 1
        else:
            placeholder += 1
    usage = getattr(provider, "usage", None)
    return contexts, {
        "imageAssets": len(contexts),
        "captionedImageAssets": captioned,
        "placeholderImageAssets": placeholder,
        "skippedImageAssets": skipped,
        "provider": provider.provider_id,
        "imageUnderstanding": "complete" if contexts and captioned == len(contexts) else "incomplete",
        "embeddedImageOcrAssets": embedded_ocr_assets,
        "embeddedImageOcrWords": embedded_ocr_words,
        "embeddedImageGridAssets": embedded_grid_assets,
        "usage": usage.as_dict() if usage is not None else None,
    }


def run_tesseract(image_path: Path, *extra_args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tesseract", str(image_path), "stdout", "--psm", "11", *extra_args],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )


def parse_tesseract_tsv(tsv: str, width: int, height: int) -> tuple[list[dict[str, Any]], list[str]]:
    words: list[dict[str, Any]] = []
    lines: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    rows = [row for row in tsv.splitlines() if row.strip()]
    if len(rows) <= 1:
        return [], []
    headers = rows[0].split("\t")
    for row in rows[1:]:
        values = row.split("\t")
        if len(values) < len(headers):
            values += [""] * (len(headers) - len(values))
        item = dict(zip(headers, values, strict=False))
        text = (item.get("text") or "").strip()
        if not text:
            continue
        try:
            conf = float(item.get("conf") or -1)
            left = int(float(item.get("left") or 0))
            top = int(float(item.get("top") or 0))
            word_width = int(float(item.get("width") or 0))
            word_height = int(float(item.get("height") or 0))
        except ValueError:
            continue
        if conf < 0:
            continue
        word = {
            "text": text,
            "confidence": round(conf / 100, 4),
            "bbox": {
                "px": {"x": left, "y": top, "w": word_width, "h": word_height},
                "relative": {
                    "x": round(left / max(width, 1), 4),
                    "y": round(top / max(height, 1), 4),
                    "w": round(word_width / max(width, 1), 4),
                    "h": round(word_height / max(height, 1), 4),
                },
            },
        }
        words.append(word)
        key = (item.get("block_num") or "0", item.get("par_num") or "0", item.get("line_num") or "0")
        lines.setdefault(key, []).append(word)
    line_texts = [
        " ".join(word["text"] for word in sorted(line_words, key=lambda item: item["bbox"]["px"]["x"]))
        for line_words in sorted(lines.values(), key=lambda items: min(item["bbox"]["px"]["y"] for item in items))
    ]
    return words, line_texts


def detect_image_grid(image_bytes: bytes) -> dict[str, Any]:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return {"method": "opencv_unavailable", "tableLike": False}
    array = np.frombuffer(image_bytes, dtype=np.uint8)
    image = cv2.imdecode(array, cv2.IMREAD_GRAYSCALE)
    if image is None:
        return {"method": "opencv_decode_failed", "tableLike": False}
    height, width = image.shape[:2]
    binary = cv2.threshold(image, 200, 255, cv2.THRESH_BINARY_INV)[1]
    horizontal_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (max(width // 30, 12), 1))
    vertical_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, max(height // 12, 12)))
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, horizontal_kernel)
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, vertical_kernel)

    def _line_boxes(mask: Any, orientation: str) -> list[dict[str, Any]]:
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes: list[dict[str, Any]] = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if orientation == "horizontal" and w < width * 0.12:
                continue
            if orientation == "vertical" and h < height * 0.12:
                continue
            boxes.append(
                {
                    "x": int(x),
                    "y": int(y),
                    "w": int(w),
                    "h": int(h),
                    "relative": {
                        "x": round(x / max(width, 1), 4),
                        "y": round(y / max(height, 1), 4),
                        "w": round(w / max(width, 1), 4),
                        "h": round(h / max(height, 1), 4),
                    },
                }
            )
        return sorted(boxes, key=lambda box: (box["y"], box["x"]))

    horizontal_lines = _line_boxes(horizontal, "horizontal")
    vertical_lines = _line_boxes(vertical, "vertical")
    return {
        "method": "opencv_morphology",
        "tableLike": len(horizontal_lines) >= 2 and len(vertical_lines) >= 2,
        "horizontalLineCount": len(horizontal_lines),
        "verticalLineCount": len(vertical_lines),
        "horizontalLines": horizontal_lines,
        "verticalLines": vertical_lines,
    }


def extract_embedded_image_content(asset: MediaAsset) -> dict[str, Any]:
    if not asset.mime_type.startswith("image/") or not asset.binary:
        return {"method": "not_image", "text": None}
    with tempfile.NamedTemporaryFile(suffix=asset.extension or ".png") as handle:
        handle.write(asset.binary)
        handle.flush()
        text_result = run_tesseract(Path(handle.name))
        tsv_result = run_tesseract(Path(handle.name), "tsv")
    text = "\n".join(line.strip() for line in text_result.stdout.splitlines() if line.strip())
    width = 0
    height = 0
    try:
        from PIL import Image
        import io

        with Image.open(io.BytesIO(asset.binary)) as image:
            width, height = image.size
    except Exception:
        width = 0
        height = 0
    words, lines = parse_tesseract_tsv(tsv_result.stdout, width, height)
    return {
        "method": "tesseract_ocr_plus_opencv_grid",
        "text": text or None,
        "lines": lines,
        "words": words,
        "wordCount": len(words),
        "averageConfidence": (
            round(sum(word["confidence"] for word in words) / len(words), 4)
            if words
            else None
        ),
        "imageSizePx": {"w": width, "h": height},
        "grid": detect_image_grid(asset.binary),
        "errors": {
            "text": text_result.stderr.strip() or None,
            "tsv": tsv_result.stderr.strip() or None,
        },
    }


def build_asset_index(
    assets: dict[str, Any],
    image_contexts: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    out: list[dict[str, Any]] = []
    for asset in sorted(assets.values(), key=lambda item: item.asset_id):
        filename = f"{asset.asset_id}{asset.extension}"
        local_path = ASSET_DIR / filename
        if asset.binary:
            local_path.write_bytes(asset.binary)
        out.append(
            {
                "assetId": asset.asset_id,
                "kind": asset.kind,
                "mimeType": asset.mime_type,
                "sizeBytes": asset.size_bytes,
                "sha256": asset.sha256,
                "path": str(local_path),
                "sourceParts": sorted(asset.source_parts),
                "usedBy": asset.used_by,
                "imageContext": (image_contexts or {}).get(asset.asset_id),
            }
        )
    return out


def extract_relevant_xml(source: Path) -> list[dict[str, str]]:
    XML_DIR.mkdir(parents=True, exist_ok=True)
    extracted: list[dict[str, str]] = []
    with zipfile.ZipFile(source) as zf:
        for name in sorted(zf.namelist()):
            if not name.startswith("ppt/"):
                continue
            if not (name.endswith(".xml") or name.endswith(".rels")):
                continue
            target = XML_DIR / name
            target.parent.mkdir(parents=True, exist_ok=True)
            data = zf.read(name)
            target.write_bytes(data)
            extracted.append(
                {
                    "sourcePart": name,
                    "path": str(target),
                    "sha256": sha256_bytes(data),
                    "sizeBytes": len(data),
                    "category": xml_category(name),
                }
            )
    return extracted


def xml_category(name: str) -> str:
    if name == "ppt/presentation.xml":
        return "presentation"
    if name.startswith("ppt/slides/"):
        return "slide"
    if name.startswith("ppt/slideLayouts/"):
        return "slideLayout"
    if name.startswith("ppt/slideMasters/"):
        return "slideMaster"
    if name.startswith("ppt/notesSlides/"):
        return "notesSlide"
    if name.startswith("ppt/theme/"):
        return "theme"
    if name.startswith("ppt/charts/"):
        return "chart"
    if name.endswith(".rels"):
        return "relationships"
    return "other"


def slide_relationships(source: Path, slide_part: str) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    with zipfile.ZipFile(source) as zf:
        for rel_id, rel in sorted(rels_for(zf, slide_part).items()):
            target = rel["target"]
            out.append(
                {
                    "id": rel_id,
                    "type": rel["type"],
                    "target": target,
                    "resolvedTarget": normalize_part_target(slide_part, target),
                }
            )
    return out


def flatten_runs(paragraphs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    for paragraph_index, paragraph in enumerate(paragraphs):
        for run_index, run in enumerate(paragraph.get("runs") or []):
            if not run.get("text"):
                continue
            runs.append(
                {
                    "paragraphIndex": paragraph_index,
                    "runIndex": run_index,
                    "text": run.get("text"),
                    "font": run.get("font") or {},
                    "color": run.get("color"),
                    "resolvedFrom": run.get("resolvedFrom") or {},
                }
            )
    return runs


def text_lines_from_runs(paragraphs: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for run in paragraph.get("runs") or []:
            text = run.get("text") or ""
            if text == "\n":
                if current.strip():
                    lines.append(current.strip())
                current = ""
            else:
                current += text
        if current.strip():
            lines.append(current.strip())
        current = ""
    return lines


def paragraph_plain_text(paragraph: dict[str, Any]) -> str:
    return "".join(
        run.get("text") or ""
        for run in paragraph.get("runs") or []
        if run.get("text") not in {"\n", "\t"}
    ).strip()


def is_watermark_text(text: str) -> bool:
    tokens = re.findall(r"[a-z]+", text.lower())
    return bool(tokens) and set(tokens).issubset({"example", "draft", "sample"})


def semantic_title_candidate(
    slide: dict[str, Any],
    repeated_titles: set[str],
) -> tuple[str | None, str | None]:
    title_structure = (slide.get("slideFormat") or {}).get("titleStructure") or {}
    current = str(slide.get("title") or "").strip()
    for line in title_structure.get("lines") or []:
        line = str(line).strip()
        if line and line.lower() not in repeated_titles and not is_watermark_text(line):
            return line, "title_placeholder"

    text_elements = sorted(
        [
            element
            for element in slide.get("elements") or []
            if element.get("type") == "text"
            and element.get("kind") == "text"
            and (element.get("source") or {}).get("placeholderType") not in {"title", "ctrTitle", "subTitle"}
        ],
        key=lambda item: (item.get("bbox") or {}).get("px", {}).get("y", 0),
    )
    fallback: tuple[str, str] | None = None
    for element in text_elements:
        for paragraph in (element.get("text") or {}).get("paragraphs") or []:
            text = paragraph_plain_text(paragraph)
            if len(text) < 4 or is_watermark_text(text):
                continue
            if text.lower() == current.lower() or text.lower() in repeated_titles:
                continue
            bullet = paragraph.get("bullet") or {}
            if paragraph.get("alignment") == "center" or bullet.get("kind") in {None, "none"}:
                return text, "first_prominent_body_paragraph"
            if fallback is None:
                fallback = (text, "first_body_paragraph")
    if fallback is not None:
        return fallback
    return None, None


def apply_semantic_titles(slides: list[dict[str, Any]]) -> None:
    title_counts: dict[str, int] = {}
    for slide in slides:
        title = str(slide.get("title") or "").strip().lower()
        if title:
            title_counts[title] = title_counts.get(title, 0) + 1
    repeated_titles = {
        title
        for title, count in title_counts.items()
        if count >= max(3, round(len(slides) * 0.25))
    }
    for slide in slides:
        original_title = str(slide.get("title") or "").strip()
        reason = "source_title"
        if not original_title or original_title.lower() in repeated_titles:
            candidate, reason = semantic_title_candidate(slide, repeated_titles)
            if candidate:
                slide["title"] = candidate
        slide.setdefault("slideFormat", {})["semanticTitle"] = {
            "value": slide.get("title") or None,
            "source": reason,
            "originalTitle": original_title or None,
            "repeatedSourceTitle": original_title.lower() in repeated_titles if original_title else False,
        }


def title_structure(elements: list[dict[str, Any]]) -> dict[str, Any] | None:
    title = next(
        (
            element
            for element in elements
            if element["type"] == "text"
            and (element.get("source") or {}).get("placeholderType") in {"title", "ctrTitle"}
        ),
        None,
    )
    if title is None:
        return None
    paragraphs = (title.get("text") or {}).get("paragraphs") or []
    return {
        "elementId": title["elementId"],
        "placeholderType": (title.get("source") or {}).get("placeholderType"),
        "bbox": title["bbox"],
        "lines": text_lines_from_runs(paragraphs),
        "runs": (title.get("text") or {}).get("runs") or [],
    }


def slide_format(
    source: Path,
    slide: dict[str, Any],
    elements: list[dict[str, Any]],
    theme: dict[str, Any] | None,
) -> dict[str, Any]:
    with zipfile.ZipFile(source) as zf:
        layout_part, master_part = slide_layout_and_master(zf, slide["sourcePart"])
    return {
        "layoutName": slide["layoutName"],
        "slideLayoutPart": layout_part,
        "slideMasterPart": master_part,
        "themeName": (theme or {}).get("themeName"),
        "themeFile": (theme or {}).get("themeFile"),
        "size": normalize_slide_size(slide["slideSizeEmu"]),
        "background": slide["background"],
        "titleStructure": title_structure(elements),
        "formatSources": {
            "slide": slide["sourcePart"],
            "layout": layout_part,
            "master": master_part,
            "theme": (theme or {}).get("themeFile"),
        },
    }


def element_quality(element: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    bbox = element["bbox"]["emu"]
    if bbox["w"] <= 0 or bbox["h"] <= 0:
        warnings.append("zero_or_negative_bbox")
    if element["type"] == "text" and not (element.get("text") or {}).get("paragraphs"):
        warnings.append("text_without_resolved_runs")
    if element["type"] == "image" and not element.get("assetId"):
        warnings.append("image_without_asset")
    return {"warnings": warnings, "reviewRequired": bool(warnings)}


BLOOM_LEVELS = [
    {
        "level": "Remember",
        "order": 1,
        "verbs": {
            "define",
            "identify",
            "label",
            "list",
            "name",
            "recall",
            "recognize",
            "state",
        },
    },
    {
        "level": "Understand",
        "order": 2,
        "verbs": {
            "classify",
            "describe",
            "discuss",
            "explain",
            "interpret",
            "summarize",
            "understand",
        },
    },
    {
        "level": "Apply",
        "order": 3,
        "verbs": {
            "apply",
            "check",
            "complete",
            "demonstrate",
            "enter",
            "fill",
            "follow",
            "perform",
            "prepare",
            "process",
            "submit",
            "use",
        },
    },
    {
        "level": "Analyze",
        "order": 4,
        "verbs": {
            "analyze",
            "compare",
            "differentiate",
            "inspect",
            "organize",
            "separate",
            "verify",
        },
    },
    {
        "level": "Evaluate",
        "order": 5,
        "verbs": {
            "approve",
            "assess",
            "decide",
            "determine",
            "evaluate",
            "judge",
            "review",
            "validate",
        },
    },
    {
        "level": "Create",
        "order": 6,
        "verbs": {
            "assemble",
            "build",
            "compose",
            "create",
            "design",
            "develop",
            "generate",
            "publish",
        },
    },
]


def slide_instructional_text(slide: dict[str, Any], elements: list[dict[str, Any]]) -> str:
    text_parts = [slide.get("title") or ""]
    notes = (slide.get("speakerNotes") or {}).get("cleaned")
    if notes:
        text_parts.append(notes)
    for element in elements:
        plain = (element.get("text") or {}).get("plain")
        if plain:
            text_parts.append(plain)
    return "\n".join(text_parts)


def infer_bloom_taxonomy(slide: dict[str, Any], elements: list[dict[str, Any]]) -> dict[str, Any]:
    text = slide_instructional_text(slide, elements)
    words = set(re.findall(r"[a-z][a-z-]+", text.lower()))
    matches: list[dict[str, Any]] = []
    for level in BLOOM_LEVELS:
        matched = sorted(words.intersection(level["verbs"]))
        if matched:
            matches.append(
                {
                    "level": level["level"],
                    "order": level["order"],
                    "matchedVerbs": matched,
                    "score": len(matched),
                }
            )

    if matches:
        selected = max(matches, key=lambda item: (item["order"], item["score"]))
        confidence = "medium" if selected["score"] >= 2 else "low"
        evidence = selected["matchedVerbs"][:6]
    else:
        selected = {"level": "Understand", "order": 2, "matchedVerbs": [], "score": 0}
        confidence = "low"
        evidence = []

    return {
        "taxonomy": "Bloom",
        "method": "deterministic_verb_heuristic",
        "status": "needs_llm_review",
        "primaryLevel": selected["level"],
        "levelOrder": selected["order"],
        "confidence": confidence,
        "evidence": evidence,
        "candidateLevels": matches,
        "llmRecommended": True,
    }


def build_geometry_document(source: Path) -> dict[str, Any]:
    with zipfile.ZipFile(source) as zf:
        theme = parse_theme(zf)

    slides, assets, stats = parse_pptx(source, theme)
    content_image_asset_ids = {
        block["assetId"]
        for slide in slides
        for block in slide["blocks"]
        if block["kind"] == "picture" and block.get("assetId")
    }
    shape_meta = shape_metadata_by_slide(source)
    inherited_decorations = inherited_decorations_by_slide(source, slides, assets, theme)
    image_contexts, image_context_stats = image_context_for_assets(assets, content_image_asset_ids)
    asset_index = build_asset_index(assets, image_contexts)
    extracted_xml = extract_relevant_xml(source)

    normalized_slides: list[dict[str, Any]] = []
    for slide in slides:
        slide_size = slide["slideSizeEmu"]
        elements: list[dict[str, Any]] = []
        for decoration_index, decoration in enumerate(
            inherited_decorations.get(slide["sourcePart"], []), start=1
        ):
            decoration_type = "image" if decoration["kind"] == "master_image" else "shape"
            element = {
                "elementId": f"{slide['unitId']}-master-{decoration_index:03d}",
                "source": {
                    "slidePart": slide["sourcePart"],
                    "shapeIndex": decoration["shapeIndex"],
                    "shapeName": decoration["shapeName"],
                    "relationshipId": None,
                    "placeholderType": None,
                    "placeholderIndex": None,
                    "tableRef": None,
                    "inheritedFrom": decoration["sourcePart"],
                },
                "type": decoration_type,
                "kind": decoration["kind"],
                "zIndex": decoration_index,
                "bbox": normalize_bbox(decoration["bbox"], slide_size),
                "bboxSource": "slide_master",
                "rotationDegrees": decoration.get("rotationDegrees", 0.0),
                "text": {"plain": None, "paragraphs": [], "runs": []},
                "assetId": decoration.get("assetId"),
                "visualStyle": decoration.get("visualStyle") or {},
                "editable": False,
                "canvas": {
                    "preferredShape": "image" if decoration_type == "image" else "geo",
                    "layer": "master_decoration",
                },
            }
            element["quality"] = element_quality(element)
            elements.append(element)
        for block in slide["blocks"]:
            paragraphs = block.get("paragraphs") or []
            plain_text = block.get("text")
            meta = shape_meta.get(slide["sourcePart"], {}).get(block["ooxmlShapeIndex"], {})
            ph_type = block.get("placeholderType") or meta.get("placeholderType")
            ph_idx = meta.get("placeholderIndex")
            resolved_bbox, bbox_source = resolved_bbox_for_block(
                source, slide["sourcePart"], block["bbox"], ph_type, ph_idx
            )
            element = {
                "elementId": block["blockId"],
                "source": {
                    "slidePart": slide["sourcePart"],
                    "shapeIndex": block["ooxmlShapeIndex"],
                    "shapeName": block.get("shapeName"),
                    "relationshipId": block.get("relationshipId"),
                    "placeholderType": ph_type,
                    "placeholderIndex": ph_idx,
                    "tableRef": block.get("tableRef"),
                },
                "type": element_type(block["kind"]),
                "kind": block["kind"],
                "zIndex": 100 + block["zOrder"],
                "bbox": normalize_bbox(resolved_bbox, slide_size),
                "bboxSource": bbox_source,
                "rotationDegrees": meta.get("rotationDegrees", 0.0),
                "text": {
                    "plain": plain_text,
                    "paragraphs": paragraphs,
                    "runs": flatten_runs(paragraphs),
                    "rows": block.get("rows") or [],
                    "table": block.get("table"),
                },
                "assetId": block.get("assetId"),
                "imageExtraction": (
                    (image_contexts.get(block.get("assetId")) or {}).get("embeddedExtraction")
                    if block.get("assetId")
                    else None
                ),
                "style": block.get("style") or {},
                "editable": block["kind"] == "text",
                "canvas": {
                    "preferredShape": "text" if block["kind"] == "text" else "image" if block["kind"] == "picture" else "geo",
                    "layer": "editable_content" if block["kind"] == "text" else "source_asset" if block["kind"] == "picture" else "structural_placeholder",
                },
            }
            element["quality"] = element_quality(element)
            elements.append(
                element
            )

        normalized_slides.append(
            {
                "slideId": slide["unitId"],
                "slideNumber": slide["slideNumber"],
                "index": slide["index"],
                "title": slide["title"],
                "layoutName": slide["layoutName"],
                "sourcePart": slide["sourcePart"],
                "size": normalize_slide_size(slide_size),
                "background": slide["background"],
                "decorations": slide["decorations"],
                "speakerNotes": slide["speakerNotes"],
                "relationships": slide_relationships(source, slide["sourcePart"]),
                "slideFormat": slide_format(source, slide, elements, theme),
                "elements": elements,
                "instructionalMetadata": {
                    "bloom": infer_bloom_taxonomy(slide, elements),
                },
            }
        )

    apply_semantic_titles(normalized_slides)

    return {
        "artifactKind": "captify.pptx.ooxmlGeometry.v1",
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "source": {
            "path": str(source),
            "originalFileName": source.name,
            "sha256": sha256_file(source),
            "sizeBytes": source.stat().st_size,
            "rendererUsed": False,
            "conversionUsed": False,
            "watermarkRisk": False,
        },
        "coordinateSystem": {
            "native": "OOXML EMU",
            "origin": "top-left",
            "emuPerInch": EMU_PER_INCH,
            "pxPerInch": PX_PER_INCH,
            "targetSlideWidthPx": TARGET_SLIDE_WIDTH_PX,
        },
        "theme": theme,
        "stats": {
            **stats,
            "contentElementCount": sum(len(slide["blocks"]) for slide in slides),
            "masterDecorationElementCount": sum(
                len(inherited_decorations.get(slide["sourcePart"], [])) for slide in slides
            ),
            "elementCount": sum(len(slide["elements"]) for slide in normalized_slides),
            "textElementCount": sum(
                1 for slide in slides for block in slide["blocks"] if block["kind"] == "text"
            ),
            "imageElementCount": sum(
                1 for slide in slides for block in slide["blocks"] if block["kind"] == "picture"
            ),
            "tableElementCount": sum(
                1 for slide in slides for block in slide["blocks"] if block["kind"] == "table"
            ),
            "tableStylesResolved": sum(
                1
                for slide in normalized_slides
                for element in slide["elements"]
                if element["type"] == "table" and ((element.get("text") or {}).get("table") or {}).get("styleDefinition")
            ),
            "tableCellsWithEffectiveStyle": sum(
                1
                for slide in normalized_slides
                for element in slide["elements"]
                if element["type"] == "table"
                for row in (((element.get("text") or {}).get("table") or {}).get("rows") or [])
                for cell in row.get("cells", [])
                if cell.get("effectiveStyle")
            ),
            "reviewRequiredElementCount": sum(
                1
                for slide in normalized_slides
                for element in slide["elements"]
                if element["quality"]["reviewRequired"]
            ),
            "imageContext": image_context_stats,
        },
        "assets": asset_index,
        "xmlParts": extracted_xml,
        "slides": normalized_slides,
    }


def asset_by_id(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {asset["assetId"]: asset for asset in document.get("assets", [])}


def build_canvas_contract(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "contractKind": "captify.canvas.deepDocument.v1",
        "source": document["source"],
        "coordinateSystem": document["coordinateSystem"],
        "layers": [
            "slide_frame",
            "background",
            "master_decoration",
            "source_asset",
            "editable_content",
            "structural_placeholder",
            "quality_overlay",
        ],
        "units": [
            {
                "unitId": slide["slideId"],
                "unitType": "slide",
                "sourcePart": slide["sourcePart"],
                "title": slide["title"],
                "size": slide["size"],
                "background": slide["background"],
                "slideFormat": slide["slideFormat"],
                "speakerNotes": slide["speakerNotes"],
                "instructionalMetadata": slide["instructionalMetadata"],
                "shapes": [
                    {
                        "shapeId": element["elementId"],
                        "shapeType": element["canvas"]["preferredShape"],
                        "layer": element["canvas"]["layer"],
                        "bbox": element["bbox"]["px"],
                        "zIndex": element["zIndex"],
                        "editable": element["editable"],
                        "text": element["text"],
                        "assetId": element["assetId"],
                        "source": element["source"],
                        "quality": element["quality"],
                    }
                    for element in slide["elements"]
                ],
            }
            for slide in document["slides"]
        ],
    }


def next_index(value: int) -> str:
    return f"a{value:04d}"


def build_tldraw(document: dict[str, Any]) -> dict[str, Any]:
    records: list[dict[str, Any]] = [
        {
            "id": "document:document",
            "typeName": "document",
            "gridSize": 10,
            "name": "",
            "meta": {},
        },
        {
            "id": "page:page1",
            "typeName": "page",
            "name": "Prototype OOXML Geometry",
            "index": "a1",
            "meta": {},
        },
    ]
    counter = 1
    gap = 80
    assets = asset_by_id(document)
    for slide in document["slides"]:
        size = slide["size"]["px"]
        row = (slide["index"]) // 2
        col = (slide["index"]) % 2
        slide_x = col * (size["w"] + gap)
        slide_y = row * (size["h"] + 130)

        records.append(
            {
                "id": f"shape:{slide['slideId']}-frame",
                "typeName": "shape",
                "type": "geo",
                "parentId": "page:page1",
                "index": next_index(counter),
                "x": slide_x,
                "y": slide_y,
                "rotation": 0,
                "isLocked": True,
                "opacity": 1,
                "meta": {"slideId": slide["slideId"], "role": "slide_frame"},
                "props": {
                    "w": size["w"],
                    "h": size["h"],
                    "geo": "rectangle",
                    "color": "black",
                    "fill": "solid",
                    "dash": "draw",
                    "size": "s",
                },
            }
        )
        counter += 1

        for element in slide["elements"]:
            bbox = element["bbox"]["px"]
            if bbox["w"] <= 0 or bbox["h"] <= 0:
                continue
            shape_type = "text" if element["type"] == "text" else "image" if element["type"] == "image" and element.get("assetId") else "geo"
            props: dict[str, Any]
            if shape_type == "text":
                props = {
                    "color": "black",
                    "size": "s",
                    "w": bbox["w"],
                    "text": element.get("text", {}).get("plain") or "",
                    "font": "draw",
                    "textAlign": "start",
                    "autoSize": False,
                    "scale": 1,
                }
            elif shape_type == "image":
                asset = assets[element["assetId"]]
                tldraw_asset_id = f"asset:{asset['assetId']}"
                if not any(record.get("id") == tldraw_asset_id for record in records):
                    records.append(
                        {
                            "id": tldraw_asset_id,
                            "typeName": "asset",
                            "type": "image",
                            "props": {
                                "name": Path(asset["path"]).name,
                                "src": Path(asset["path"]).resolve().as_uri(),
                                "w": bbox["w"],
                                "h": bbox["h"],
                                "mimeType": asset["mimeType"],
                                "isAnimated": False,
                            },
                            "meta": {"sourceAssetId": asset["assetId"]},
                        }
                    )
                props = {
                    "w": bbox["w"],
                    "h": bbox["h"],
                    "assetId": tldraw_asset_id,
                    "playing": True,
                    "url": "",
                    "crop": None,
                }
            else:
                is_master_shape = element["kind"] == "master_shape"
                visual_style = element.get("visualStyle") or {}
                props = {
                    "w": bbox["w"],
                    "h": bbox["h"],
                    "geo": "rectangle",
                    "color": "black" if is_master_shape else "blue",
                    "fill": "solid" if is_master_shape and bbox["h"] <= 8 else "none",
                    "dash": "draw" if is_master_shape else "dashed",
                    "size": "s",
                }
                if is_master_shape and visual_style.get("fillColor"):
                    props["metaColor"] = visual_style["fillColor"]
            records.append(
                {
                    "id": f"shape:{element['elementId']}",
                    "typeName": "shape",
                    "type": shape_type,
                    "parentId": "page:page1",
                    "index": next_index(counter),
                    "x": slide_x + bbox["x"],
                    "y": slide_y + bbox["y"],
                    "rotation": 0,
                    "isLocked": not element["editable"],
                    "opacity": 1,
                    "meta": {
                        "slideId": slide["slideId"],
                        "elementId": element["elementId"],
                        "kind": element["kind"],
                        "layer": element["canvas"]["layer"],
                    },
                    "props": props,
                }
            )
            counter += 1

    return {
        "tldrawFileFormatVersion": 1,
        "schema": {
            "schemaVersion": 2,
            "storeVersion": 4,
            "recordVersions": {
                "document": {"version": 2},
                "page": {"version": 1},
                "shape": {
                    "version": 4,
                    "subTypeKey": "type",
                    "subTypeVersions": {"geo": 10, "image": 4, "text": 2},
                },
                "asset": {"version": 1, "subTypeKey": "type", "subTypeVersions": {"image": 3}},
            },
        },
        "records": records,
    }


def html_escape(value: Any) -> str:
    return (
        str("" if value is None else value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def css_font_family(family: str | None) -> str:
    if not family:
        return "Arial, sans-serif"
    safe = family.replace("'", "").replace('"', "")
    fallback = "serif" if "Times" in safe else "sans-serif"
    return f"'{safe}', {fallback}"


def text_style_color(style: dict[str, Any] | None) -> str | None:
    text_style = style or {}
    color = ((text_style.get("color") or {}).get("value"))
    if color:
        return str(color)
    font_ref_color = (((text_style.get("fontRef") or {}).get("color") or {}).get("value"))
    return str(font_ref_color) if font_ref_color else None


def run_style(run: dict[str, Any], inherited_text_style: dict[str, Any] | None = None) -> str:
    font = run.get("font") or {}
    size = font.get("size") or 14
    family = css_font_family(font.get("family"))
    weight = "700" if font.get("weight") == "bold" else "400"
    if inherited_text_style and inherited_text_style.get("bold") in {"1", "true", "on"}:
        weight = "700"
    elif inherited_text_style and inherited_text_style.get("bold") in {"0", "false", "off"}:
        weight = "400"
    italic = "italic" if font.get("italic") else "normal"
    if inherited_text_style and inherited_text_style.get("italic") in {"1", "true", "on"}:
        italic = "italic"
    elif inherited_text_style and inherited_text_style.get("italic") in {"0", "false", "off"}:
        italic = "normal"
    underline = "underline" if font.get("underline") and font.get("underline") != "none" else "none"
    color = text_style_color(inherited_text_style) or run.get("color") or "#111827"
    return (
        f"font-family:{family};font-size:{float(size):.2f}pt;font-weight:{weight};"
        f"font-style:{italic};text-decoration:{underline};color:{html_escape(color)};"
    )


def paragraph_align(paragraph: dict[str, Any], element: dict[str, Any]) -> str:
    if paragraph.get("alignment"):
        return str(paragraph["alignment"])
    placeholder = (element.get("source") or {}).get("placeholderType")
    if placeholder in {"title", "ctrTitle", "subTitle"}:
        return "center"
    return "left"


def paragraph_font_size_pt(paragraph: dict[str, Any]) -> float:
    sizes = [
        float(((run.get("font") or {}).get("size") or 0))
        for run in paragraph.get("runs") or []
        if ((run.get("font") or {}).get("size") or 0)
    ]
    empty_size = (((paragraph.get("emptyRunStyle") or {}).get("font") or {}).get("size"))
    if empty_size:
        sizes.append(float(empty_size))
    return max(sizes) if sizes else 14.0


def paragraph_spacing_pt(spacing: dict[str, Any] | None, font_size_pt: float) -> float:
    if not spacing:
        return 0.0
    if spacing.get("kind") == "points":
        return float(spacing.get("points") or 0)
    if spacing.get("kind") == "percent":
        return font_size_pt * float(spacing.get("ratio") or 0)
    return 0.0


def paragraph_line_height_css(spacing: dict[str, Any] | None) -> str:
    if not spacing:
        return "normal"
    if spacing.get("kind") == "points":
        return f"{float(spacing.get('points') or 0):.2f}pt"
    if spacing.get("kind") == "percent":
        ratio = float(spacing.get("ratio") or 1)
        return f"{max(ratio, 0.1):.3f}"
    return "normal"


def paragraph_html(paragraph: dict[str, Any], element: dict[str, Any]) -> str:
    runs = paragraph.get("runs") or []
    spans: list[str] = []
    previous_text = ""
    for run in runs:
        text = run.get("text") or ""
        if text == "\t":
            text = " "
        if text == "\n":
            if spans:
                spans.append("<br>")
                previous_text = ""
            continue
        if not text:
            continue
        if (
            previous_text
            and not previous_text[-1].isspace()
            and not text[0].isspace()
            and previous_text[-1].isalnum()
            and text[0].isalnum()
        ):
            text = " " + text
        spans.append(f'<span style="{run_style(run)}">{html_escape(text)}</span>')
        previous_text = text
    if not spans:
        if not paragraph.get("empty"):
            return ""
        empty_style = paragraph.get("emptyRunStyle") or {"font": {"size": 14}}
        spans.append(f'<span style="{run_style(empty_style)}">&nbsp;</span>')
    bullet = paragraph.get("bullet") or {}
    if bullet.get("kind") == "char" and bullet.get("char"):
        spans.insert(0, f'<span style="{run_style(runs[0]) if runs else ""}">{html_escape(bullet["char"])} </span>')
    elif bullet.get("kind") == "number":
        spans.insert(0, f'<span style="{run_style(runs[0]) if runs else ""}">1. </span>')
    align = paragraph_align(paragraph, element)
    indent = int(paragraph.get("indentLevel") or 0)
    margin_left = indent * 24
    margin_left_emu = paragraph.get("marginLeftEmu")
    indent_emu = paragraph.get("indentEmu")
    if margin_left_emu is not None:
        margin_left = max(0, round(int(margin_left_emu) / EMU_PER_INCH * PX_PER_INCH, 2))
    text_indent = 0
    if indent_emu is not None:
        text_indent = round(int(indent_emu) / EMU_PER_INCH * PX_PER_INCH, 2)
    font_size_pt = paragraph_font_size_pt(paragraph)
    margin_top = paragraph_spacing_pt(paragraph.get("spacingBefore"), font_size_pt)
    margin_bottom = paragraph_spacing_pt(paragraph.get("spacingAfter"), font_size_pt)
    line_height = paragraph_line_height_css(paragraph.get("lineSpacing"))
    return (
        f'<p style="margin:{margin_top:.2f}pt 0 {margin_bottom:.2f}pt {margin_left}px;'
        f'text-align:{html_escape(align)};text-indent:{text_indent}px;'
        f'line-height:{line_height};">{"".join(spans)}</p>'
    )


def text_element_html(element: dict[str, Any]) -> str:
    paragraphs = (element.get("text") or {}).get("paragraphs") or []
    rendered = "".join(paragraph_html(paragraph, element) for paragraph in paragraphs)
    if rendered:
        return rendered
    text = html_escape((element.get("text") or {}).get("plain"))
    return f"<p style='margin:0;font-size:14pt;line-height:1.1'>{text}</p>"


def style_color(style: dict[str, Any] | None) -> str | None:
    color = ((style or {}).get("color") or {}).get("value")
    return str(color) if color else None


def fill_css(fill: dict[str, Any] | None) -> str | None:
    if not fill or fill.get("kind") == "none":
        return None
    color = style_color(fill)
    return f"background:{html_escape(color)};" if color else None


def border_css(line: dict[str, Any] | None, side: str) -> str | None:
    if not line:
        return None
    color = style_color((line.get("fill") or {}))
    width = max(1, round((line.get("widthEmu") or 9525) / EMU_PER_INCH * PX_PER_INCH, 2))
    return f"border-{side}:{width}px solid {html_escape(color or '#111827')};"


def run_list_html(runs: list[dict[str, Any]], inherited_text_style: dict[str, Any] | None = None) -> str:
    spans: list[str] = []
    previous_text = ""
    for run in runs:
        text = run.get("text") or ""
        if text == "\n":
            spans.append("<br>")
            previous_text = ""
            continue
        if text == "\t":
            text = " "
        if (
            previous_text
            and text
            and not previous_text[-1].isspace()
            and not text[0].isspace()
            and previous_text[-1].isalnum()
            and text[0].isalnum()
        ):
            text = " " + text
        spans.append(f'<span style="{run_style(run, inherited_text_style)}">{html_escape(text)}</span>')
        previous_text = text
    return "".join(spans)


def merge_table_styles(*styles: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {"borders": {}}
    for style in styles:
        if not style:
            continue
        if style.get("fill"):
            merged["fill"] = style["fill"]
        borders = style.get("borders") or {}
        for side, line in borders.items():
            if line:
                merged.setdefault("borders", {})[side] = line
    return merged


def table_cell_html(cell: dict[str, Any], *, header: bool, inherited_style: dict[str, Any] | None = None) -> str:
    tag = "th" if header else "td"
    style = cell.get("effectiveStyle") or merge_table_styles(inherited_style, cell.get("style") or {})
    css_parts = [fill_css(style.get("fill"))]
    inherited_text_style = style.get("text") or {}
    borders = style.get("borders") or {}
    for side in ("left", "right", "top", "bottom"):
        css_parts.append(border_css(borders.get(side), side))
    paragraphs = cell.get("paragraphs") or []
    body = ""
    if paragraphs:
        rendered = []
        for paragraph in paragraphs:
            runs = paragraph.get("runs") or []
            if runs:
                rendered.append(f"<p>{run_list_html(runs, inherited_text_style)}</p>")
        body = "".join(rendered)
    if not body:
        inherited_color = text_style_color(inherited_text_style)
        style_attr = f" style='color:{html_escape(inherited_color)};'" if inherited_color else ""
        body = f"<span{style_attr}>{html_escape(cell.get('text') or '').replace(chr(10), '<br>')}</span>"
    css = "".join(part for part in css_parts if part)
    return f"<{tag} style='{css}'>{body}</{tag}>"


def table_element_html(element: dict[str, Any]) -> str:
    table = (element.get("text") or {}).get("table") or {}
    rows = table.get("rows") or []
    if not rows:
        rows = [
            {"rowIndex": index, "cells": [{"text": text} for text in row]}
            for index, row in enumerate((element.get("text") or {}).get("rows") or [])
        ]
    if not rows:
        text = html_escape((element.get("text") or {}).get("plain"))
        return f"<pre class='table-fallback'>{text}</pre>"
    row_html = []
    max_cols = max((len(row.get("cells") or []) for row in rows), default=1)
    first_row_header = ((table.get("properties") or {}).get("firstRow")) is not False
    style_parts = ((table.get("styleDefinition") or {}).get("parts") or {})
    for row_index, row in enumerate(rows):
        inherited_style = None
        if first_row_header and row_index == 0:
            inherited_style = style_parts.get("firstRow")
        elif (table.get("properties") or {}).get("bandRow"):
            inherited_style = style_parts.get("band1H" if row_index % 2 == 1 else "band2H")
        if not inherited_style:
            inherited_style = style_parts.get("wholeTbl")
        cells = [
            table_cell_html(
                cell,
                header=bool(first_row_header and row_index == 0),
                inherited_style=inherited_style,
            )
            for cell in row.get("cells") or []
        ]
        empty_tag = "th" if first_row_header and row_index == 0 else "td"
        for _ in range(max_cols - len(cells)):
            cells.append(f"<{empty_tag}></{empty_tag}>")
        height = round((row.get("heightEmu") or 0) / EMU_PER_INCH * PX_PER_INCH, 2)
        style = f"height:{height}px;" if height else ""
        row_html.append(f"<tr style='{style}'>{''.join(cells)}</tr>")
    return f"<table class='extracted-table'>{''.join(row_html)}</table>"


def bboxes_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return not (
        a["x"] + a["w"] <= b["x"]
        or b["x"] + b["w"] <= a["x"]
        or a["y"] + a["h"] <= b["y"]
        or b["y"] + b["h"] <= a["y"]
    )


def horizontal_overlap_ratio(a: dict[str, Any], b: dict[str, Any]) -> float:
    overlap = max(0.0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    return overlap / max(min(a["w"], b["w"]), 1)


def preview_text_bbox(element: dict[str, Any], slide_elements: list[dict[str, Any]]) -> dict[str, Any]:
    bbox = dict(element["bbox"]["px"])
    if element["type"] != "text":
        return bbox
    placeholder = (element.get("source") or {}).get("placeholderType")
    if placeholder in {"title", "ctrTitle", "subTitle"}:
        return bbox

    top = bbox["y"]
    candidates: list[float] = []
    for other in slide_elements:
        if other["kind"] == "master_image" or other["type"] != "image":
            continue
        other_box = other["bbox"]["px"]
        if not bboxes_overlap(bbox, other_box):
            continue
        if horizontal_overlap_ratio(bbox, other_box) < 0.25:
            continue
        available_above = other_box["y"] - top - 10
        if available_above >= 80 and other_box["y"] > top + 40:
            candidates.append(available_above)
    if candidates:
        bbox["h"] = round(max(24.0, min(bbox["h"], min(candidates))), 2)
    return bbox


def safe_text_padding_style(
    element: dict[str, Any],
    slide_elements: list[dict[str, Any]],
    render_bbox: dict[str, Any] | None = None,
) -> str:
    bbox = render_bbox or element["bbox"]["px"]
    left = 4.0
    right = 4.0
    center = bbox["x"] + bbox["w"] / 2
    for other in slide_elements:
        if other["kind"] == "master_image" or other["type"] != "image":
            continue
        other_box = other["bbox"]["px"]
        if not bboxes_overlap(bbox, other_box):
            continue
        other_center = other_box["x"] + other_box["w"] / 2
        if other_center < center:
            left = max(left, other_box["x"] + other_box["w"] - bbox["x"] + 8)
        else:
            right = max(right, bbox["x"] + bbox["w"] - other_box["x"] + 8)
    return f"padding-left:{left:.2f}px;padding-right:{right:.2f}px;"


def master_shape_style(element: dict[str, Any]) -> str:
    visual_style = element.get("visualStyle") or {}
    fill = visual_style.get("fillColor") or visual_style.get("lineColor") or "#111827"
    return f"background:{html_escape(fill)};border:0;"


def element_classes(element: dict[str, Any]) -> str:
    classes = ["el"]
    classes.append(
        "text"
        if element["type"] == "text"
        else "image"
        if element["type"] == "image"
        else "table"
        if element["type"] == "table"
        else "box"
    )
    if element["kind"] == "master_shape":
        classes.append("master-shape")
    if element["kind"] == "master_image":
        classes.append("master-image")
    if element.get("quality", {}).get("reviewRequired"):
        classes.append("needs-review")
    placeholder = (element.get("source") or {}).get("placeholderType")
    if placeholder:
        classes.append(f"ph-{placeholder}")
    return " ".join(classes)


def notes_html(slide: dict[str, Any]) -> str:
    notes = (slide.get("speakerNotes") or {}).get("cleaned")
    if not notes:
        return "<p class='empty'>No speaker notes extracted.</p>"
    paragraphs = [
        line.strip()
        for line in notes.splitlines()
        if line.strip()
    ]
    return "".join(f"<p>{html_escape(paragraph)}</p>" for paragraph in paragraphs)


def image_context_html(slide: dict[str, Any], assets: dict[str, dict[str, Any]]) -> str:
    seen: set[str] = set()
    items: list[str] = []
    for element in slide["elements"]:
        if element["type"] != "image" or element["kind"] == "master_image" or not element.get("assetId"):
            continue
        asset_id = element["assetId"]
        if asset_id in seen:
            continue
        seen.add(asset_id)
        asset = assets.get(asset_id) or {}
        context = asset.get("imageContext") or {}
        text = context.get("text")
        reason = context.get("reason")
        body = text or reason or "Image context not available."
        provider = context.get("provider") or "unknown"
        embedded = context.get("embeddedExtraction") or {}
        embedded_lines = embedded.get("lines") or []
        embedded_html = ""
        if embedded.get("text"):
            line_items = "".join(
                f"<li>{html_escape(line)}</li>"
                for line in embedded_lines[:12]
            )
            grid = embedded.get("grid") or {}
            embedded_html = (
                "<details class='json-detail' open>"
                "<summary>Embedded image extraction</summary>"
                f"<p>OCR words: {html_escape(embedded.get('wordCount'))}; "
                f"avg confidence: {html_escape(embedded.get('averageConfidence'))}; "
                f"grid lines: H{html_escape(grid.get('horizontalLineCount'))} / V{html_escape(grid.get('verticalLineCount'))}</p>"
                f"<ul>{line_items}</ul>"
                "</details>"
            )
        shape_name = (element.get("source") or {}).get("shapeName") or element.get("kind")
        items.append(
            "<div class='image-context-item'>"
            f"<div class='image-context-title'>{html_escape(shape_name)}</div>"
            f"<p>{html_escape(body)}</p>"
            f"{embedded_html}"
            f"<div class='bloom-meta'>Provider: {html_escape(provider)}</div>"
            "</div>"
        )
    if not items:
        return "<p class='empty'>No image assets on this slide.</p>"
    return "".join(items)


def has_content_images(slide: dict[str, Any]) -> bool:
    return any(
        element["type"] == "image" and element["kind"] != "master_image" and element.get("assetId")
        for element in slide["elements"]
    )


def bloom_html(slide: dict[str, Any]) -> str:
    bloom = ((slide.get("instructionalMetadata") or {}).get("bloom") or {})
    evidence = bloom.get("evidence") or []
    evidence_html = (
        "".join(f"<span>{html_escape(item)}</span>" for item in evidence)
        if evidence
        else "<span>needs LLM review</span>"
    )
    return (
        f"<div class='bloom-level'>{html_escape(bloom.get('primaryLevel') or 'Unclassified')}</div>"
        "<div class='bloom-meta'>Method: deterministic verb heuristic</div>"
        "<div class='bloom-meta'>Review: Bedrock enrichment recommended</div>"
        f"<div class='bloom-meta'>Confidence: {html_escape(bloom.get('confidence'))}</div>"
        f"<div class='chips'>{evidence_html}</div>"
    )


def json_pretty_html(data: Any) -> str:
    return f"<pre class='json-block'>{html_escape(json.dumps(data, indent=2, sort_keys=True))}</pre>"


def json_color_html(data: Any) -> str:
    return f"<pre class='json-block json-colored'>{json_value_html(data)}</pre>"


def json_value_html(value: Any, indent: int = 0) -> str:
    pad = "  " * indent
    next_pad = "  " * (indent + 1)
    if isinstance(value, dict):
        if not value:
            return "<span class='json-punct'>{}</span>"
        items = [
            f"{next_pad}<span class='json-key'>&quot;{html_escape(key)}&quot;</span>"
            f"<span class='json-punct'>: </span>{json_value_html(item, indent + 1)}"
            for key, item in value.items()
        ]
        return (
            "<span class='json-punct'>{</span>\n"
            + ",\n".join(items)
            + f"\n{pad}<span class='json-punct'>}}</span>"
        )
    if isinstance(value, list):
        if not value:
            return "<span class='json-punct'>[]</span>"
        items = [f"{next_pad}{json_value_html(item, indent + 1)}" for item in value]
        return (
            "<span class='json-punct'>[</span>\n"
            + ",\n".join(items)
            + f"\n{pad}<span class='json-punct'>]</span>"
        )
    if isinstance(value, str):
        return f"<span class='json-string'>&quot;{html_escape(value)}&quot;</span>"
    if isinstance(value, bool):
        return f"<span class='json-bool'>{str(value).lower()}</span>"
    if value is None:
        return "<span class='json-null'>null</span>"
    return f"<span class='json-number'>{html_escape(value)}</span>"


def source_item_json_html(slide: dict[str, Any]) -> str:
    items: list[str] = []
    for element in slide["elements"]:
        summary = f"{element['elementId']} / {element['type']} / {element['kind']}"
        items.append(
            "<details class='json-detail'>"
            f"<summary>Source Item JSON: {html_escape(summary)}</summary>"
            f"{json_pretty_html(element)}"
            "</details>"
        )
    return "".join(items)


def slide_elements_html(slide: dict[str, Any], assets: dict[str, dict[str, Any]]) -> str:
    elements_html: list[str] = []
    for element in slide["elements"]:
        bbox = element["bbox"]["px"]
        if element["type"] == "text":
            bbox = preview_text_bbox(element, slide["elements"])
        style = (
            f"left:{bbox['x']}px;top:{bbox['y']}px;width:{bbox['w']}px;"
            f"height:{bbox['h']}px;z-index:{element['zIndex']};"
        )
        data_attrs = f'data-element-id="{html_escape(element["elementId"])}"'
        if element["type"] == "text":
            style += safe_text_padding_style(element, slide["elements"], bbox)
            elements_html.append(
                f'<div {data_attrs} class="{element_classes(element)}" style="{style}">'
                f'<div class="text-inner">{text_element_html(element)}</div></div>'
            )
        elif element["type"] == "table":
            elements_html.append(
                f'<div {data_attrs} class="{element_classes(element)}" style="{style}">'
                f"{table_element_html(element)}</div>"
            )
        elif element["type"] == "image" and element.get("assetId") in assets:
            asset = assets[element["assetId"]]
            rel = Path(asset["path"]).resolve().relative_to(OUT.resolve())
            elements_html.append(
                f'<img {data_attrs} class="{element_classes(element)}" style="{style}" src="{html_escape(rel)}" />'
            )
        elif element["kind"] == "master_shape":
            style += master_shape_style(element)
            elements_html.append(
                f'<div {data_attrs} class="{element_classes(element)}" style="{style}"></div>'
            )
        else:
            if not element.get("quality", {}).get("reviewRequired"):
                continue
            label = html_escape(element["kind"])
            elements_html.append(
                f'<div {data_attrs} class="{element_classes(element)}" style="{style}">{label}</div>'
            )
    return "".join(elements_html)


def slide_canvas_html(
    slide: dict[str, Any],
    assets: dict[str, dict[str, Any]],
    *,
    scale: float,
) -> str:
    size = slide["size"]["px"]
    background = (slide.get("background") or {}).get("color") or "#fff"
    preview_width = round(size["w"] * scale, 2)
    preview_height = round(size["h"] * scale, 2)
    return (
        f"<div class='slide-shell' style='width:{preview_width}px;height:{preview_height}px'>"
        f"<div class='slide' style='width:{size['w']}px;height:{size['h']}px;"
        f"background:{background};transform:scale({scale});'>"
        f"{slide_elements_html(slide, assets)}</div></div>"
    )


def preview_style() -> str:
    return (
        "body{font-family:Arial,sans-serif;background:#eef1f5;margin:0;padding:24px;color:#111827}"
        "h1{max-width:1600px;margin:0 auto 24px}.review-row{display:grid;grid-template-columns:340px 520px minmax(360px,1fr);gap:18px;align-items:start;margin:0 auto 32px;max-width:1600px}"
        "h2{font-size:13px;margin:0 0 8px}.pane-title{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:#374151;margin:0 0 8px}.slide-shell{position:relative;overflow:hidden;box-shadow:0 1px 5px #0003;background:white}"
        ".slide{position:relative;overflow:hidden;transform-origin:top left}"
        ".png-panel,.json-panel{background:#fff;border:1px solid #d1d5db;border-radius:6px;padding:12px;box-shadow:0 1px 3px #0001;overflow:auto}.png-panel img{width:100%;height:auto;display:block;border:1px solid #e5e7eb;background:white}.png-missing{aspect-ratio:4/3;display:flex;align-items:center;justify-content:center;background:#f9fafb;border:1px solid #e5e7eb;color:#6b7280;font-size:13px;text-align:center;padding:12px}.slide-column{min-width:0}.json-panel{max-height:760px}"
        ".el{position:absolute;box-sizing:border-box;overflow:hidden}.text{color:#111827;padding:2px 4px}"
        ".text-inner{transform-origin:top left;width:100%}"
        ".ph-title,.ph-ctrTitle{display:flex;align-items:flex-start;justify-content:center;padding-top:4px}"
        ".ph-subTitle{display:flex;align-items:center;justify-content:center}"
        ".box{border:1px dashed #2563eb;color:#2563eb;font-size:11px;padding:2px;background:#eff6ff66}"
        ".table{background:#ffffffcc;border:1px solid #111827;padding:0;overflow:hidden}.extracted-table{width:100%;height:100%;border-collapse:collapse;table-layout:fixed;font-family:'Times New Roman',serif;font-size:13px;color:#111827}.extracted-table th,.extracted-table td{border:1px solid #111827;padding:3px 5px;vertical-align:top;overflow:hidden;word-break:break-word}.extracted-table th{font-weight:700}.extracted-table p{margin:0;line-height:1.05}.table-fallback{margin:0;padding:6px;font-size:12px;white-space:pre-wrap;font-family:'Times New Roman',serif}"
        ".master-shape{border:0;background:#111827}.master-image{pointer-events:none}"
        ".image{object-fit:contain}.needs-review{outline:2px solid #dc2626;outline-offset:-2px;background:#fee2e266}"
        ".review-panel{background:#fff;border:1px solid #d1d5db;border-radius:6px;padding:14px;box-shadow:0 1px 3px #0001;min-height:180px;overflow:auto}"
        ".panel-block+.panel-block{border-top:1px solid #e5e7eb;margin-top:14px;padding-top:14px}"
        "h3{font-size:12px;text-transform:uppercase;letter-spacing:.04em;margin:0 0 8px;color:#374151}.review-panel p,.json-panel p{font-size:13px;line-height:1.35;margin:0 0 8px}.empty{color:#6b7280;font-style:italic}"
        ".image-context-item{margin-bottom:10px}.image-context-title{font-size:12px;font-weight:700;color:#111827;margin-bottom:3px}"
        ".bloom-level{font-size:20px;font-weight:700;margin-bottom:6px}.bloom-meta{font-size:12px;color:#4b5563;margin-bottom:4px}.chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}.chips span{font-size:12px;background:#eef2ff;color:#312e81;border:1px solid #c7d2fe;border-radius:999px;padding:3px 8px}"
        ".json-detail{border:1px solid #e5e7eb;border-radius:4px;margin:6px 0;background:#f9fafb}.json-detail summary{cursor:pointer;font-size:12px;font-weight:700;padding:6px 8px;color:#111827}.json-block{max-height:360px;overflow:auto;margin:0;padding:8px;background:#111827;color:#e5e7eb;font-size:11px;line-height:1.35;white-space:pre-wrap}"
        ".json-panel>.json-block{max-height:none}.json-key{color:#93c5fd}.json-string{color:#86efac}.json-number{color:#fbbf24}.json-bool{color:#f0abfc}.json-null{color:#fca5a5}.json-punct{color:#d1d5db}"
        "@media(max-width:1300px){.review-row{grid-template-columns:1fr}.png-panel,.json-panel,.review-panel{width:auto;max-height:none}}"
    )


def render_slide_png_source_html(slide: dict[str, Any], assets: dict[str, dict[str, Any]]) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<style>{preview_style()}body{{margin:0;padding:0;background:white}}"
        ".slide-shell{box-shadow:none}</style></head><body>"
        f"{slide_canvas_html(slide, assets, scale=1)}"
        "<script>"
        "function fitText(){document.querySelectorAll('.text').forEach(function(box){"
        "var inner=box.querySelector('.text-inner');if(!inner)return;inner.style.transform='';"
        "var bw=Math.max(1,box.clientWidth-8),bh=Math.max(1,box.clientHeight-4);"
        "var scale=Math.min(1,bw/Math.max(1,inner.scrollWidth),bh/Math.max(1,inner.scrollHeight));"
        "if(scale<1){inner.style.transform='scale('+scale+')';inner.style.width=(100/scale)+'%';}});}"
        "window.addEventListener('load',fitText);</script></body></html>"
    )


def normalized_match_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in {"the", "and", "for", "with", "that", "this", "form"}
    }


def slide_match_text(slide: dict[str, Any]) -> str:
    parts = [str(slide.get("title") or "")]
    for element in slide.get("elements") or []:
        if element.get("type") not in {"text", "table"}:
            continue
        text = element.get("text") or {}
        if isinstance(text, dict):
            parts.append(str(text.get("plain") or ""))
        elif isinstance(text, str):
            parts.append(text)
    notes = slide.get("speakerNotes") or {}
    parts.append(str(notes.get("cleaned") or notes.get("raw") or ""))
    return " ".join(parts)


def pdf_page_texts(pdf_source: Path) -> list[str]:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return []
    pdf = pdfium.PdfDocument(str(pdf_source))
    texts: list[str] = []
    for page_index in range(len(pdf)):
        page = pdf[page_index]
        texts.append(" ".join(page.get_textpage().get_text_range().split()))
    return texts


def match_pdf_pages_to_slides(
    document: dict[str, Any],
    pdf_texts: list[str],
) -> dict[str, dict[str, Any]]:
    page_tokens = [normalized_match_tokens(text) for text in pdf_texts]
    matches: dict[str, dict[str, Any]] = {}
    start_page = 0
    for slide in document["slides"]:
        slide_tokens = normalized_match_tokens(slide_match_text(slide))
        if not slide_tokens:
            continue
        best_page = None
        best_score = 0.0
        search_end = min(len(page_tokens), start_page + 8)
        for page_index in range(start_page, search_end):
            tokens = page_tokens[page_index]
            if not tokens:
                continue
            overlap = len(slide_tokens & tokens)
            score = overlap / max(len(slide_tokens), 1)
            if score > best_score:
                best_score = score
                best_page = page_index
        if best_page is None or best_score < 0.18:
            continue
        matches[slide["slideId"]] = {
            "pageIndex": best_page,
            "pageNumber": best_page + 1,
            "score": round(best_score, 4),
            "textPreview": pdf_texts[best_page][:240],
        }
        start_page = best_page + 1
    return matches


def render_pdf_reference_pages(
    pdf_source: Path,
    matches: dict[str, dict[str, Any]],
) -> dict[str, str]:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return {}
    pdf = pdfium.PdfDocument(str(pdf_source))
    refs: dict[str, str] = {}
    SLIDE_PNG_DIR.mkdir(parents=True, exist_ok=True)
    for slide_id, match in matches.items():
        page_index = int(match["pageIndex"])
        if page_index < 0 or page_index >= len(pdf):
            continue
        page = pdf[page_index]
        bitmap = page.render(scale=1.6)
        image = bitmap.to_pil()
        png_path = SLIDE_PNG_DIR / f"{slide_id}.png"
        image.save(png_path)
        refs[slide_id] = str(png_path.resolve().relative_to(OUT.resolve()))
    return refs


def generate_pdf_slide_png_references(document: dict[str, Any], pdf_source: Path) -> tuple[dict[str, str], dict[str, Any]]:
    if not pdf_source.exists():
        return {}, {"status": "missing_pdf", "source": str(pdf_source)}
    texts = pdf_page_texts(pdf_source)
    if not texts:
        return {}, {"status": "pdf_text_unavailable", "source": str(pdf_source)}
    matches = match_pdf_pages_to_slides(document, texts)
    refs = render_pdf_reference_pages(pdf_source, matches)
    unmatched = [
        slide["slideId"]
        for slide in document["slides"]
        if slide["slideId"] not in refs
    ]
    return refs, {
        "status": "complete",
        "source": str(pdf_source),
        "pdfPageCount": len(texts),
        "slideCount": len(document["slides"]),
        "matchedSlideCount": len(refs),
        "unmatchedSlideIds": unmatched,
        "matches": matches,
    }


def slide_element_counts(slide: dict[str, Any]) -> dict[str, int]:
    counts = {"text": 0, "image": 0, "contentImage": 0, "table": 0, "reviewRequired": 0}
    for element in slide.get("elements") or []:
        element_type = element.get("type")
        if element_type in counts:
            counts[element_type] += 1
        if element_type == "image" and element.get("kind") != "master_image":
            counts["contentImage"] += 1
        if (element.get("quality") or {}).get("reviewRequired"):
            counts["reviewRequired"] += 1
    return counts


def paragraph_counts(slide: dict[str, Any]) -> dict[str, int]:
    total = 0
    bullet = 0
    with_metrics = 0
    for element in slide.get("elements") or []:
        for paragraph in (element.get("text") or {}).get("paragraphs") or []:
            if not paragraph_plain_text(paragraph):
                continue
            total += 1
            if (paragraph.get("bullet") or {}).get("kind") in {"char", "number"}:
                bullet += 1
            if paragraph.get("marginLeftEmu") is not None or paragraph.get("indentEmu") is not None:
                with_metrics += 1
    return {"total": total, "bullets": bullet, "withMarginOrIndent": with_metrics}


def build_extraction_comparison(
    document: dict[str, Any],
    course_model: dict[str, Any],
    pdf_reference_map: dict[str, Any],
    pdf_texts: list[str],
) -> dict[str, Any]:
    course_slides = {
        slide["slideId"]: slide
        for slide in ((course_model.get("course") or {}).get("slides") or [])
    }
    matches = pdf_reference_map.get("matches") or {}
    title_counts: dict[str, int] = {}
    for slide in document.get("slides") or []:
        title = str(slide.get("title") or "").strip().lower()
        if title:
            title_counts[title] = title_counts.get(title, 0) + 1
    repeated_titles = {title for title, count in title_counts.items() if count >= 3}

    records: list[dict[str, Any]] = []
    issue_counts: dict[str, int] = {}
    for slide in document.get("slides") or []:
        slide_id = slide["slideId"]
        match = matches.get(slide_id) or {}
        page_index = match.get("pageIndex")
        pdf_text = pdf_texts[page_index] if isinstance(page_index, int) and page_index < len(pdf_texts) else ""
        pdf_tokens = normalized_match_tokens(pdf_text)
        extracted_tokens = normalized_match_tokens(slide_match_text(slide))
        shared_tokens = pdf_tokens & extracted_tokens
        pdf_coverage = round(len(shared_tokens) / max(len(pdf_tokens), 1), 4) if pdf_tokens else 0.0
        extracted_precision = round(len(shared_tokens) / max(len(extracted_tokens), 1), 4) if extracted_tokens else 0.0
        element_counts = slide_element_counts(slide)
        paragraph_metrics = paragraph_counts(slide)
        semantic_title = (slide.get("slideFormat") or {}).get("semanticTitle") or {}
        course_slide = course_slides.get(slide_id) or {}
        issues = []
        if not match:
            issues.append("missing_pdf_match")
        elif float(match.get("score") or 0) < 0.25:
            issues.append("low_pdf_match_score")
        if pdf_tokens and pdf_coverage < 0.55:
            issues.append("low_pdf_token_coverage")
        if not str(slide.get("title") or "").strip():
            issues.append("missing_semantic_title")
        if semantic_title.get("repeatedSourceTitle") and semantic_title.get("value") == semantic_title.get("originalTitle"):
            issues.append("repeated_header_title")
        if element_counts["text"] == 0:
            issues.append("no_text_elements")
        if element_counts["reviewRequired"]:
            issues.append("review_required_elements")
        if paragraph_metrics["bullets"] and paragraph_metrics["withMarginOrIndent"] == 0:
            issues.append("bullet_indent_metrics_missing")
        for issue in issues:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1
        records.append(
            {
                "slideId": slide_id,
                "slideNumber": slide.get("slideNumber"),
                "title": slide.get("title"),
                "semanticTitle": semantic_title,
                "pdfPageNumber": match.get("pageNumber"),
                "pdfMatchScore": match.get("score"),
                "tokenComparison": {
                    "pdfTokenCount": len(pdf_tokens),
                    "extractedTokenCount": len(extracted_tokens),
                    "sharedTokenCount": len(shared_tokens),
                    "pdfTokenCoverage": pdf_coverage,
                    "extractedTokenPrecision": extracted_precision,
                },
                "elementCounts": element_counts,
                "paragraphMetrics": paragraph_metrics,
                "courseModel": {
                    "primaryRole": course_slide.get("primaryRole"),
                    "role": course_slide.get("role"),
                    "bloomSignal": course_slide.get("bloomSignal"),
                },
                "issues": issues,
            }
        )
    lowest_coverage = min(
        (record["tokenComparison"]["pdfTokenCoverage"] for record in records),
        default=0.0,
    )
    return {
        "artifactKind": "captify.extractionComparison.v1",
        "source": {
            "pptx": str(SOURCE),
            "pdfReference": str(PDF_REFERENCE_SOURCE),
        },
        "summary": {
            "slideCount": len(records),
            "pdfPageCount": len(pdf_texts),
            "matchedSlideCount": sum(1 for record in records if record["pdfPageNumber"] is not None),
            "slidesWithIssues": sum(1 for record in records if record["issues"]),
            "lowestPdfTokenCoverage": round(lowest_coverage, 4),
            "issueCounts": issue_counts,
        },
        "slides": records,
    }


def generate_slide_png_references(document: dict[str, Any]) -> dict[str, str]:
    chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
    if not chrome:
        return {}
    SLIDE_PNG_DIR.mkdir(parents=True, exist_ok=True)
    temp_dir = OUT / "_slide_png_html"
    temp_dir.mkdir(parents=True, exist_ok=True)
    assets = asset_by_id(document)
    refs: dict[str, str] = {}
    for slide in document["slides"]:
        size = slide["size"]["px"]
        html_path = temp_dir / f"{slide['slideId']}.html"
        png_path = SLIDE_PNG_DIR / f"{slide['slideId']}.png"
        html_path.write_text(render_slide_png_source_html(slide, assets))
        command = [
            chrome,
            "--headless",
            "--disable-gpu",
            "--no-sandbox",
            f"--screenshot={png_path}",
            f"--window-size={int(size['w'])},{int(size['h'])}",
            html_path.resolve().as_uri(),
        ]
        try:
            subprocess.run(command, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
        except Exception:
            continue
        if png_path.exists():
            refs[slide["slideId"]] = str(png_path.resolve().relative_to(OUT.resolve()))
    shutil.rmtree(temp_dir, ignore_errors=True)
    return refs


def render_preview_html(
    document: dict[str, Any],
    course_model: dict[str, Any] | None = None,
    multi_format_summary: dict[str, Any] | None = None,
    slide_png_refs: dict[str, str] | None = None,
    extraction_comparison: dict[str, Any] | None = None,
) -> str:
    assets = asset_by_id(document)
    course_slides = {
        slide["slideId"]: slide
        for slide in ((course_model or {}).get("course") or {}).get("slides", [])
    }
    slides_html: list[str] = []
    slide_png_refs = slide_png_refs or {}
    comparison_by_slide = {
        slide["slideId"]: slide
        for slide in ((extraction_comparison or {}).get("slides") or [])
    }
    preview_scale = 0.54
    for slide in document["slides"]:
        size = slide["size"]["px"]
        preview_height = round(size["h"] * preview_scale, 2)
        course_slide = course_slides.get(slide["slideId"])
        png_ref = slide_png_refs.get(slide["slideId"])
        png_html = (
            f"<img src='{html_escape(png_ref)}' alt='Original slide PNG for {html_escape(slide['slideId'])}' />"
            if png_ref
            else "<div class='png-missing'>Original slide PNG was not generated.</div>"
        )
        slide_json = {
            "courseModel": course_slide,
            "extractionComparison": comparison_by_slide.get(slide["slideId"]),
            "sourceSlide": slide,
        }
        json_html = (
            json_color_html(slide_json)
            if course_slide
            else "<p class='empty'>No course-model slide record generated.</p>"
        )
        slides_html.append(
            f"<section class='review-row'>"
            f"<div class='png-panel'><h2>{html_escape(slide['slideId'])}: {html_escape(slide['title'])}</h2>"
            f"<div class='pane-title'>Original Slide PNG (PDF Reference)</div>{png_html}</div>"
            f"<div class='slide-column'><div class='pane-title'>Extracted Slide Render</div>"
            f"{slide_canvas_html(slide, assets, scale=preview_scale)}</div>"
            f"<aside class='json-panel' style='max-height:{max(preview_height, 520)}px'>"
            f"<div class='pane-title'>Slide JSON Object: Course Model JSON + Source Slide JSON</div>{json_html}"
            f"<div class='panel-block'><h3>Speaker Notes</h3>{notes_html(slide)}</div>"
            f"{f'<div class=\"panel-block\"><h3>Image Context</h3>{image_context_html(slide, assets)}</div>' if has_content_images(slide) else ''}"
            f"<div class='panel-block'><h3>Bloom Taxonomy</h3>{bloom_html(slide)}</div>"
            f"<div class='panel-block'><h3>Source Item JSON</h3>{source_item_json_html(slide)}</div>"
            f"</aside></section>"
        )
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Prototype OOXML preview</title>"
        f"<style>{preview_style()}</style></head><body><h1>Prototype OOXML Geometry Preview</h1>"
        "<p style='max-width:1600px;margin:0 auto 18px;font-size:13px;color:#374151'>"
        "Audit artifacts: pptx-ooxml-geometry.json, canvas-contract.json, "
        "course-model.json, course-analysis-summary.json, reengineering-input.json, "
        "enriched-manifest.json, extraction-comparison-summary.json, multi-format-summary.json, multi-format/*, "
        "and schemas/*.schema.json."
        "</p>"
        f"<section class='review-row'><div class='png-panel'><h2>Multi-format Course Model Coverage</h2>"
        f"{json_pretty_html(multi_format_summary or {})}</div>"
        "<div class='review-panel'><div class='panel-block'><h3>Coverage</h3>"
        "<p>PPTX, DOCX, multi-sheet XLSX, and PDF manifests are processed through the same course-model writer.</p>"
        "</div></div><div class='json-panel'><div class='pane-title'>Coverage JSON</div>"
        f"{json_color_html(multi_format_summary or {})}</div></section>"
        f"{''.join(slides_html)}"
        "<script>"
        "function fitText(){document.querySelectorAll('.text').forEach(function(box){"
        "var inner=box.querySelector('.text-inner');if(!inner)return;"
        "inner.style.transform='';inner.style.width='100%';"
        "var bw=Math.max(1,box.clientWidth-8),bh=Math.max(1,box.clientHeight-4);"
        "var scale=Math.min(1,bw/Math.max(1,inner.scrollWidth),bh/Math.max(1,inner.scrollHeight));"
        "if(scale<1){inner.style.transform='scale('+scale+')';inner.style.width=(100/scale)+'%';}"
        "});}"
        "window.addEventListener('load',fitText);"
        "</script></body></html>"
    )


def build_multi_format_summary(pptx_manifest: dict[str, Any]) -> dict[str, Any]:
    manifests = {"pptx": pptx_manifest}
    for file_type, source in MULTI_FORMAT_SOURCES.items():
        manifests[file_type] = manifest_for_file(source)

    results = {}
    for file_type, manifest in manifests.items():
        paths = write_course_artifacts(
            manifest,
            OUT / "multi-format" / file_type,
            source_manifest_key=f"{file_type}-manifest.json",
        )
        course_model = json.loads(paths.course_model.read_text())
        results[file_type] = {
            "artifactKind": manifest.get("artifactKind"),
            "unitCount": len(manifest.get("slides") or manifest.get("units") or []),
            "courseModel": str(paths.course_model),
            "courseAnalysisSummary": str(paths.course_analysis_summary),
            "reengineeringInput": str(paths.reengineering_input),
            "enrichedManifest": str(paths.enriched_manifest),
            "schemaDir": str(paths.schemas_dir),
            "moduleCount": len(course_model["course"]["modules"]),
            "slideRecordCount": len(course_model["course"]["slides"]),
            "objectiveCount": len(course_model["course"]["objectives"]),
            "assessmentCount": len(course_model["course"]["assessments"]),
            "reengineeringCandidateCount": len(course_model["course"]["reengineeringCandidates"]),
            "llmRequests": course_model["providerUsage"]["llmRequests"],
            "totalTokens": course_model["providerUsage"]["totalTokens"],
        }
    return {
        "status": "complete",
        "fileTypes": results,
    }


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True, exist_ok=True)
    document = build_geometry_document(SOURCE)
    course_artifacts = build_course_artifacts(
        document,
        source_manifest_key=str(OUT / "pptx-ooxml-geometry.json"),
    )
    course_model = course_artifacts["courseModel"]
    course_summary = course_artifacts["courseAnalysisSummary"]
    reengineering_input = course_artifacts["reengineeringInput"]
    enriched_manifest = course_artifacts["enrichedManifest"]
    canvas_contract = build_canvas_contract(document)
    tldraw = build_tldraw(document)

    geometry_path = OUT / "pptx-ooxml-geometry.json"
    canvas_contract_path = OUT / "canvas-contract.json"
    tldraw_path = OUT / "pptx-ooxml-geometry.tldr"
    preview_path = OUT / "preview.html"
    summary_path = OUT / "summary.json"
    course_model_path = OUT / "course-model.json"
    course_summary_path = OUT / "course-analysis-summary.json"
    reengineering_input_path = OUT / "reengineering-input.json"
    enriched_manifest_path = OUT / "enriched-manifest.json"
    extraction_comparison_path = OUT / "extraction-comparison-summary.json"
    write_json(geometry_path, document)
    write_json(canvas_contract_path, canvas_contract)
    write_json(tldraw_path, tldraw)
    write_json(course_model_path, course_model)
    write_json(course_summary_path, course_summary)
    write_json(reengineering_input_path, reengineering_input)
    write_json(enriched_manifest_path, enriched_manifest)
    multi_format_summary = build_multi_format_summary(document)
    multi_format_summary_path = OUT / "multi-format-summary.json"
    write_json(multi_format_summary_path, multi_format_summary)
    schema_dir = OUT / "schemas"
    schema_dir.mkdir(parents=True, exist_ok=True)
    for schema_file in resources.files("docling_serve.powerpoint_courseware.schemas").iterdir():
        if schema_file.name.endswith(".schema.json"):
            shutil.copyfile(schema_file, schema_dir / schema_file.name)
    slide_png_refs, pdf_reference_map = generate_pdf_slide_png_references(document, PDF_REFERENCE_SOURCE)
    slide_png_source = "pdf_reference"
    if len(slide_png_refs) != len(document["slides"]):
        fallback_refs = generate_slide_png_references(document)
        slide_png_refs = {**fallback_refs, **slide_png_refs}
        slide_png_source = "pdf_reference_with_html_fallback"
    pdf_reference_map_path = OUT / "pdf-reference-map.json"
    write_json(pdf_reference_map_path, pdf_reference_map)
    pdf_texts = pdf_page_texts(PDF_REFERENCE_SOURCE) if PDF_REFERENCE_SOURCE.exists() else []
    extraction_comparison = build_extraction_comparison(
        document,
        course_model,
        pdf_reference_map,
        pdf_texts,
    )
    write_json(extraction_comparison_path, extraction_comparison)
    preview_path.write_text(
        render_preview_html(
            document,
            course_model,
            multi_format_summary,
            slide_png_refs=slide_png_refs,
            extraction_comparison=extraction_comparison,
        )
    )
    write_json(
        summary_path,
        {
            "status": "complete",
            "slideCount": document["stats"]["slideCount"],
            "elementCount": document["stats"]["elementCount"],
            "textElementCount": document["stats"]["textElementCount"],
            "imageElementCount": document["stats"]["imageElementCount"],
            "tableElementCount": document["stats"]["tableElementCount"],
            "tableStylesResolved": document["stats"]["tableStylesResolved"],
            "tableCellsWithEffectiveStyle": document["stats"]["tableCellsWithEffectiveStyle"],
            "assetCount": len(document["assets"]),
            "xmlPartCount": len(document["xmlParts"]),
            "reviewRequiredElementCount": document["stats"]["reviewRequiredElementCount"],
            "imageContext": document["stats"]["imageContext"],
            "courseModel": {
                "moduleCount": len(course_model["course"]["modules"]),
                "objectiveCount": len(course_model["course"]["objectives"]),
                "assessmentCount": len(course_model["course"]["assessments"]),
                "reengineeringCandidateCount": len(course_model["course"]["reengineeringCandidates"]),
                "providerUsage": course_model["providerUsage"],
            },
            "rendererUsed": False,
            "slidePngReferenceCount": len(slide_png_refs),
            "slidePngReferenceSource": slide_png_source,
            "pdfReference": {
                "source": str(PDF_REFERENCE_SOURCE),
                "pageCount": pdf_reference_map.get("pdfPageCount"),
                "matchedSlideCount": pdf_reference_map.get("matchedSlideCount"),
                "unmatchedSlideIds": pdf_reference_map.get("unmatchedSlideIds"),
            },
            "extractionComparison": extraction_comparison["summary"],
            "artifacts": {
                "geometryJson": str(geometry_path),
                "canvasContract": str(canvas_contract_path),
                "tldrawFile": str(tldraw_path),
                "previewHtml": str(preview_path),
                "summary": str(summary_path),
                "courseModel": str(course_model_path),
                "courseAnalysisSummary": str(course_summary_path),
                "reengineeringInput": str(reengineering_input_path),
                "enrichedManifest": str(enriched_manifest_path),
                "extractionComparison": str(extraction_comparison_path),
                "multiFormatSummary": str(multi_format_summary_path),
                "schemas": str(schema_dir),
                "slidePngReferences": str(SLIDE_PNG_DIR),
                "pdfReferenceMap": str(pdf_reference_map_path),
            },
        },
    )
    print((summary_path).read_text())


if __name__ == "__main__":
    main()
