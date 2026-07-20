"""Vision page classifier for scanned technical orders.

When a scan has no usable text layer AND no detectable parts-table header, there
is no cheap way to find the parts section or the drawing pages. This module asks
the multimodal model (Sonnet 4.5 via the LiteLLM/Bedrock proxy) to classify each
page from a low-resolution thumbnail — 'parts' (a repair-parts / illustrated-parts
LIST table), 'drawing' (an engineering illustration / exploded view / schematic),
or 'other' (prose, TOC, procedures).

Thumbnails are tiny (the model only needs the page LAYOUT, not the text) and many
are sent per request, so a whole document classifies in a few cheap calls. The
result seeds the vision parts reader's candidate pages and lets every drawing be
captured even when its OCR caption was lost.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import tempfile
from pathlib import Path

import httpx

from docling_serve.technical_order.figure_hotspots import (
    _downscale_png,
    render_figure_png,
)

_log = logging.getLogger(__name__)

_PROMPT = (
    "You are shown low-resolution thumbnails of CONSECUTIVE pages from a U.S. "
    "military technical order, each labeled '--- Page N ---'. Classify EACH page "
    "by its visual LAYOUT:\n"
    "- 'parts': a repair-parts / illustrated-parts-breakdown LIST table — many "
    "rows in aligned columns (index, part number, CAGE/FSCM, NSN, description, "
    "quantity).\n"
    "- 'drawing': an engineering ILLUSTRATION — exploded view, assembly drawing, "
    "schematic, wiring/block diagram, with callout numbers or graphics.\n"
    "- 'other': running text, title page, table of contents, procedures, warnings.\n"
    "Return STRICT JSON only for the pages shown, in order:\n"
    '{"pages":[{"page":N,"type":"parts|drawing|other"}]}'
)

_VALID = {"parts", "drawing", "other"}


def _thumb(pdf_path: Path, page: int, tmp: Path, dpi: int) -> bytes | None:
    png = render_figure_png(pdf_path, page, tmp / f"thumb-{page}", dpi=dpi)
    if png is None:
        return None
    return _downscale_png(png, max_dim=300)


def _classify_batch(
    items: list[tuple[int, bytes]],
    *,
    base_url: str,
    api_key: str,
    model: str,
    timeout: float,
) -> dict[int, str]:
    content: list[dict] = [{"type": "text", "text": _PROMPT}]
    for page, data in items:
        content.append({"type": "text", "text": f"--- Page {page} ---"})
        content.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{base64.b64encode(data).decode()}"
                },
            }
        )
    body = {
        "model": model,
        "max_tokens": 2048,
        "temperature": 0,
        "messages": [{"role": "user", "content": content}],
    }
    try:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=body,
            headers={"authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        text = str(
            (((resp.json().get("choices") or [{}])[0]).get("message") or {}).get(
                "content"
            )
            or ""
        )
    except Exception as err:
        _log.info("page classify batch failed: %s", err)
        return {}
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    out: dict[int, str] = {}
    for row in data.get("pages") or []:
        if not isinstance(row, dict):
            continue
        try:
            page_value = row.get("page")
            if page_value is None:
                continue
            page = int(page_value)
        except (TypeError, ValueError):
            continue
        kind = str(row.get("type") or "").strip().lower()
        if kind in _VALID:
            out[page] = kind
    return out


def classify_pages(
    pdf_path: Path,
    page_count: int,
    *,
    base_url: str,
    api_key: str,
    model: str,
    dpi: int = 60,
    batch: int = 10,
    max_pages: int = 160,
    timeout: float = 180.0,
    work_dir: Path | None = None,
) -> dict[int, str]:
    """Classify every page (up to ``max_pages``) as parts|drawing|other.

    Returns ``{page_number: type}``. Best-effort: pages that fail to render or
    classify are simply absent from the map.
    """
    if not (base_url and api_key and model) or page_count <= 0:
        return {}
    tmp = work_dir or Path(tempfile.mkdtemp(prefix="page-classify-"))
    tmp.mkdir(parents=True, exist_ok=True)

    result: dict[int, str] = {}
    pages = list(range(1, min(page_count, max_pages) + 1))
    for i in range(0, len(pages), batch):
        items: list[tuple[int, bytes]] = []
        for page in pages[i : i + batch]:
            data = _thumb(pdf_path, page, tmp, dpi)
            if data is not None:
                items.append((page, data))
        if items:
            result.update(
                _classify_batch(
                    items,
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    timeout=timeout,
                )
            )
    return result
