from __future__ import annotations

import argparse
import json
from pathlib import Path
from textwrap import wrap
from typing import Any

from PIL import Image, ImageDraw, ImageFont


LEVEL_COLORS = {
    "remember": (191, 219, 254),
    "understand": (147, 197, 253),
    "apply": (187, 247, 208),
    "analyze": (254, 240, 138),
    "evaluate": (253, 186, 116),
    "create": (221, 214, 254),
}


def font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf" if bold else "",
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf" if bold else "",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default()


def text_lines(text: str, width_chars: int, max_lines: int) -> list[str]:
    lines: list[str] = []
    for source_line in text.splitlines() or [""]:
        lines.extend(wrap(source_line.strip(), width=width_chars) or [""])
    return lines[:max_lines]


def draw_card(
    draw: ImageDraw.ImageDraw,
    image: Image.Image,
    widget: dict[str, Any],
    classification: dict[str, Any],
) -> None:
    x, y, w, h = (int(widget[k]) for k in ["x", "y", "w", "h"])
    level = classification["bloomLevel"]
    role = classification["instructionalRole"]
    color = LEVEL_COLORS.get(level, (229, 231, 235))
    draw.rounded_rectangle((x, y, x + w, y + h), radius=8, fill=(255, 255, 255), outline=(31, 41, 55), width=2)
    draw.rounded_rectangle((x + 10, y + 10, x + w - 10, y + 34), radius=5, fill=color, outline=(148, 163, 184), width=1)
    draw.text((x + 18, y + 15), f"{level.upper()}  |  {role}", fill=(15, 23, 42), font=font(13, bold=True))

    title = widget["title"]
    title_y = y + 44
    for line in text_lines(title, 44, 2):
        draw.text((x + 14, title_y), line, fill=(15, 23, 42), font=font(15, bold=True))
        title_y += 18

    slide_ref = widget.get("builtinPayload", {}).get("slideImageRef")
    preview_box = (x + 14, y + 88, x + w - 14, y + h - 72)
    draw.rectangle(preview_box, fill=(241, 245, 249), outline=(203, 213, 225), width=1)
    if slide_ref and Path(slide_ref).exists():
        slide = Image.open(slide_ref).convert("RGB")
        max_w = preview_box[2] - preview_box[0] - 8
        max_h = preview_box[3] - preview_box[1] - 8
        slide.thumbnail((max_w, max_h), Image.Resampling.LANCZOS)
        px = preview_box[0] + (max_w - slide.width) // 2 + 4
        py = preview_box[1] + (max_h - slide.height) // 2 + 4
        image.paste(slide, (px, py))
    else:
        draw.text((preview_box[0] + 12, preview_box[1] + 12), "No render image", fill=(100, 116, 139), font=font(14))

    notes = widget.get("builtinPayload", {}).get("speakerNotes") or ""
    footer = "notes" if notes else "no notes"
    footer += f"  confidence {classification['confidence']:.2f}"
    draw.text((x + 14, y + h - 50), footer, fill=(51, 65, 85), font=font(13))

    improvements = classification.get("recommendedImprovements") or []
    if improvements:
        suggestion = improvements[0]["suggestion"]
        draw.text((x + 14, y + h - 30), text_lines(suggestion, 52, 1)[0], fill=(120, 53, 15), font=font(11))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    artifact = json.loads(args.artifact.read_text())
    widgets = artifact["canvasProjection"]["widgets"]
    classifications = {
        item["unitId"]: item for item in artifact["trainingTaxonomy"]["unitClassifications"]
    }

    max_x = max(int(widget["x"] + widget["w"]) for widget in widgets) + 80
    max_y = max(int(widget["y"] + widget["h"]) for widget in widgets) + 100
    canvas = Image.new("RGB", (max_x, max_y), (226, 232, 240))
    draw = ImageDraw.Draw(canvas)
    draw.text((80, 30), "Deep Document Canvas Prototype", fill=(15, 23, 42), font=font(26, bold=True))
    summary = artifact["trainingTaxonomy"]["deckSummary"]
    draw.text(
        (80, 62),
        f"{summary['unitCount']} slides | dominant Bloom: {summary['dominantBloomLevel']} | higher-order slides: {summary['higherOrderCount']}",
        fill=(51, 65, 85),
        font=font(16),
    )

    for widget in widgets:
        unit_id = widget["builtinPayload"]["sourceUnitId"]
        draw_card(draw, canvas, widget, classifications[unit_id])

    args.out.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(args.out)
    print(
        json.dumps(
            {
                "preview": str(args.out),
                "size": [canvas.width, canvas.height],
                "widgets": len(widgets),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
