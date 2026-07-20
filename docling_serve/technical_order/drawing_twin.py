"""Drawing digital twin: per-part 2D geometry + assembly structure (+3D slots).

The hotspot pass locates each callout NUMBER on a figure; this stage goes
further and asks a frontier vision model (Opus-class) to trace the drawn
GEOMETRY of every called-out part — a normalized polygon region over the
part's line art, its leader line, the view type, and (for exploded views) the
explosion axis and each part's order along it. Merged with the parts list's
indenture tree, that yields ``captify.drawing-twin.v1``: an assembly graph
where every part is an individual item, related to its parent, carrying its
2D geometry per figure and reserved slots (``mesh``, ``transform3d``,
``boundingBox3d``) for the future 2D→3D reconstruction stage to fill in.

Nothing document-specific is assumed: the model is grounded with the
already-extracted callout list and hotspot boxes for the specific figure it
is reading, and its output is validated against that same list.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

SCHEMA = "captify.drawing-twin.v1"

_SYSTEM = (
    "You are a mechanical drawing analyst. You trace the drawn geometry of "
    "parts on technical-order figures (exploded views, assemblies, details). "
    "You answer ONLY with JSON matching the requested schema. Never invent "
    "parts that are not in the provided callout list; if you cannot locate a "
    'callout\'s geometry, omit it and record the index in "unlocated".'
)

_PROMPT = """This drawing is figure {figure_number} ("{figure_title}") from a
military technical order. The illustrated-parts-breakdown table lists these
callouts on this figure (index, part number, description, hotspot box of the
printed index number in normalized [x0,y0,x1,y1] image coordinates):

{callouts}

Trace the drawing like putting paper over it with a digital pencil. Return JSON:
{{
  "view": {{
    "type": "exploded" | "assembly" | "detail" | "section" | "diagram",
    "explosionAxis": [dx, dy] | null,
    "notes": "one line about the view"
  }},
  "parts": [
    {{
      "index": "<callout index from the list>",
      "region": [[x, y], ...],
      "leader": {{"from": [x, y], "to": [x, y]}} | null,
      "explodedOrder": <int, order along explosionAxis, 0 = closest to origin> | null
    }}
  ],
  "unlocated": ["<index>", ...],
  "confidence": <0.0-1.0>
}}

Rules:
- "region" is a closed polygon (4-12 points, normalized 0-1) tightly outlining
  the PART'S DRAWN GEOMETRY, not the printed callout number.
- "leader" runs from the callout number ("from") to where it touches the part
  ("to"); null when no leader line is drawn.
- "explosionAxis" is the dominant direction parts are exploded along, as a unit
  vector in image space; null unless the view is exploded.
- "explodedOrder" ranks the parts along that axis; null for non-exploded views.
- Every "index" MUST come from the provided callout list."""


def _b64(png_path: Path) -> str:
    return base64.b64encode(png_path.read_bytes()).decode("ascii")


def _extract_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def read_figure_geometry(
    png_path: Path,
    figure: dict[str, Any],
    callouts: list[dict[str, Any]],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float = 180.0,
) -> dict[str, Any] | None:
    """One vision call: per-part polygon regions for one figure sheet.

    ``callouts`` rows need ``index``, ``partNumber``, ``description`` and
    optionally ``box`` (the hotspot). Returns the parsed model payload with
    only the provided indices kept, or ``None`` on transport/parse failure.
    """
    lines = [
        f"- index {c['index']}: part {c.get('partNumber') or '?'} — "
        f"{(c.get('description') or '')[:80]}"
        + (f" (hotspot {c['box']})" if c.get("box") else "")
        for c in callouts
    ]
    prompt = _PROMPT.format(
        figure_number=figure.get("figureNumber") or "?",
        figure_title=figure.get("figureTitle") or "",
        callouts="\n".join(lines),
    )
    body = {
        "model": model,
        "max_tokens": 8192,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{_b64(png_path)}"},
                    },
                ],
            },
        ],
    }
    for attempt in (1, 2):
        try:
            resp = httpx.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=timeout,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"] or ""
            parsed = _extract_json(text)
            if parsed is not None:
                return _validate(parsed, {str(c["index"]) for c in callouts})
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("drawing-twin vision attempt %d failed: %s", attempt, exc)
    return None


def _validate(parsed: dict, allowed: set[str]) -> dict:
    """Drop hallucinated indices / malformed regions; clamp coordinates."""

    def clamp(pt):
        return [min(1.0, max(0.0, float(pt[0]))), min(1.0, max(0.0, float(pt[1])))]

    parts = []
    for p in parsed.get("parts") or []:
        idx = str(p.get("index") or "")
        region = p.get("region") or []
        if idx not in allowed or len(region) < 3:
            continue
        try:
            region = [clamp(pt) for pt in region]
            leader = p.get("leader")
            if leader:
                leader = {"from": clamp(leader["from"]), "to": clamp(leader["to"])}
        except (TypeError, ValueError, KeyError, IndexError):
            continue
        order = p.get("explodedOrder")
        parts.append(
            {
                "index": idx,
                "region": region,
                "leader": leader,
                "explodedOrder": int(order)
                if isinstance(order, (int, float))
                else None,
            }
        )
    view = parsed.get("view") or {}
    axis = view.get("explosionAxis")
    if not (isinstance(axis, list) and len(axis) == 2):
        axis = None
    return {
        "view": {
            "type": str(view.get("type") or "diagram"),
            "explosionAxis": axis,
            "notes": str(view.get("notes") or ""),
        },
        "parts": parts,
        "unlocated": [
            str(i) for i in parsed.get("unlocated") or [] if str(i) in allowed
        ],
        "confidence": float(parsed.get("confidence") or 0.0),
    }


def build_assembly_nodes(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every part as an individual assembly node with its parent relation and
    reserved 3D slots. Pure BOM restructuring — no model involved."""
    nodes = []
    for e in entries:
        nodes.append(
            {
                "sequence": e.get("sequence"),
                "parentSequence": e.get("parentSequence"),
                "partNumber": e.get("partNumberRaw") or "",
                "description": e.get("nomenclature") or e.get("descriptionRaw") or "",
                "quantity": e.get("unitsPerAssemblyRaw") or "",
                "indentureLevel": e.get("indentureLevel"),
                "figureNumber": e.get("figureNumberRaw") or "",
                "figureIndex": e.get("figureIndexRaw") or "",
                "rowType": e.get("rowType") or "",
                # 2D geometry attaches per figure in figures[].parts; these
                # slots are for the 2D→3D reconstruction stage.
                "mesh": None,
                "transform3d": None,
                "boundingBox3d": None,
            }
        )
    return nodes


def build_drawing_twin(
    bom: dict[str, Any],
    media_dir: Path,
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_figures: int = 12,
) -> dict[str, Any]:
    """The full ``captify.drawing-twin.v1`` payload for a published bundle.

    Vision-traces up to ``max_figures`` hotspot-bearing sheets (largest callout
    count first, where geometry adds the most value) and always emits the
    assembly graph derived from the parts list.
    """
    figures = [f for f in bom.get("figures") or [] if f.get("hotspots")]
    figures.sort(key=lambda f: -len(f["hotspots"]))
    entry_by_seq = {e.get("sequence"): e for e in bom.get("entries") or []}

    traced = []
    for fig in figures[: max(0, max_figures)]:
        png = media_dir / Path(str(fig.get("mediaKey") or "")).name
        if not png.is_file():
            continue
        callouts = []
        for h in fig["hotspots"]:
            entry = entry_by_seq.get(h.get("partSequence"))
            callouts.append(
                {
                    "index": h.get("index"),
                    "partNumber": h.get("partNumber"),
                    "description": (entry or {}).get("nomenclature")
                    or (entry or {}).get("descriptionRaw"),
                    "box": h.get("box"),
                }
            )
        result = read_figure_geometry(
            png, fig, callouts, base_url=base_url, api_key=api_key, model=model
        )
        if result is None:
            continue
        seq_by_index = {
            str(h.get("index")): h.get("partSequence") for h in fig["hotspots"]
        }
        for p in result["parts"]:
            p["partSequence"] = seq_by_index.get(p["index"])
        traced.append(
            {
                "figureNumber": fig.get("figureNumber"),
                "sheetNumber": fig.get("sheetNumber"),
                "mediaKey": fig.get("mediaKey"),
                "vectorKey": fig.get("vectorKey"),
                **result,
            }
        )

    return {
        "schema": SCHEMA,
        "model": model,
        "figures": traced,
        "assembly": {"nodes": build_assembly_nodes(bom.get("entries") or [])},
        "stats": {
            "figuresTraced": len(traced),
            "partRegions": sum(len(f["parts"]) for f in traced),
            "assemblyNodes": len(bom.get("entries") or []),
        },
    }
