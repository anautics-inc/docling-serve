"""Vision parts-table reader for scanned / dirty-OCR technical orders.

Scanned parts pages defeat the text-layer column parsers: a re-OCR'd page is a
noisy character soup whose column alignment is lost, so the MPL/RPSTL grammars
emit garbage part numbers. This module reads the parts TABLE directly off the
rendered page with a multimodal model (Sonnet 4.5 via the LiteLLM/Bedrock proxy)
— the model sees the printed grid, not a scrambled text stream — and returns the
same ``PartsListEntry`` rows the deterministic parsers produce.

It is the OCR counterpart to the born-digital path: used only when the document
is genuinely scanned, gated to the parts pages, and bounded by a page budget so
per-page vision spend stays predictable.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path

import httpx

from docling_serve.technical_order.figure_hotspots import (
    _downscale_png,
    render_figure_png,
)
from docling_serve.technical_order.mpl import PartsListEntry

_log = logging.getLogger(__name__)

_NSN_RE = re.compile(r"\b\d{4}-\d{2}-\d{3}-\d{4}\b")

_PROMPT = (
    "This is a scanned page from a U.S. military illustrated parts breakdown (IPB) "
    "or RPSTL parts list. Read the PARTS-LIST TABLE on the page and return every "
    "data row, in printed order.\n"
    "Each row has some of these columns (omit what isn't present):\n"
    "- index: the FIGURE & INDEX or ITEM number (e.g. '1', '5A', '14')\n"
    "- smr: the Source/Maintenance/Recoverability code (e.g. PAOZZ)\n"
    "- cage: the 5-char CAGE/FSCM code (e.g. 96906, 0W357)\n"
    "- partNumber: the manufacturer part number\n"
    "- nsn: National Stock Number, formatted ####-##-###-#### if present\n"
    "- description: the noun/name; count leading dots as the indenture level\n"
    "- qty: units per assembly\n"
    "- indenture: integer indenture level (number of leading dots), else 0\n"
    "Return STRICT JSON only:\n"
    '{"parts":[{"index":"","smr":"","cage":"","partNumber":"","nsn":"","description":"","qty":"","indenture":0}]}\n'
    "Transcribe exactly what is printed; never invent part numbers or NSNs. Skip "
    "page headers, footers, figure captions, and prose. If the page has no parts "
    'table, return {"parts":[]}.'
)


def _vision_rows(png_path: Path, *, base_url: str, api_key: str, model: str, timeout: float = 180.0):
    """One page -> list of raw row dicts from the model. [] on any failure."""
    img_b64 = base64.b64encode(_downscale_png(png_path, max_dim=2000)).decode("ascii")
    body = {
        "model": model,
        "max_tokens": 8192,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    },
                ],
            }
        ],
    }
    try:
        resp = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            json=body,
            headers={"authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        content = str(
            (((resp.json().get("choices") or [{}])[0]).get("message") or {}).get("content") or ""
        )
    except Exception as err:
        _log.info("vision parts page failed: %s", err)
        return []
    match = re.search(r"\{.*\}", content, re.S)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    rows = data.get("parts")
    return rows if isinstance(rows, list) else []


def _to_entry(row: dict, *, sequence: int, page_number: int) -> PartsListEntry | None:
    """Build a PartsListEntry from a model row; drop rows with no part identity."""
    if not isinstance(row, dict):
        return None

    def s(key: str) -> str:
        value = row.get(key)
        return str(value).strip() if value not in (None, "") else ""

    part = s("partNumber")
    nsn = s("nsn")
    desc = s("description")
    # A usable row needs at least a part number or an NSN.
    if not part and not nsn:
        return None
    nsn_norm = _NSN_RE.search(nsn) or _NSN_RE.search(s("partNumber"))
    try:
        indenture = int(row.get("indenture") or 0)
    except (TypeError, ValueError):
        indenture = desc[: len(desc) - len(desc.lstrip("."))].count(".")
    # Strip leader-dot / dash noise the model sometimes prefixes to the index
    # ("-5" -> "5", ". 5" -> "5") so it still matches a figure callout.
    index = s("index").lstrip("-. ").strip()
    return PartsListEntry(
        sequence=sequence,
        page_number=page_number,
        figure_index_raw=index,
        part_number_raw=part,
        cage_raw=s("cage"),
        description_raw=desc.lstrip(". ").strip(),
        units_per_assembly_raw=s("qty"),
        smr_raw=s("smr"),
        nsn_raw=nsn_norm.group(0) if nsn_norm else nsn,
        indenture_level=max(indenture, 0),
        row_type="part",
        review_status="vision",
    )


def vision_parse_parts(
    pdf_path: Path,
    candidate_pages: list[int],
    *,
    base_url: str,
    api_key: str,
    model: str,
    max_pages: int = 40,
    dpi: int = 200,
    work_dir: Path | None = None,
) -> tuple[list[PartsListEntry], dict]:
    """Read the parts table off each candidate page with the vision model.

    ``candidate_pages`` are the 1-based pages believed to carry the parts list
    (e.g. the pages the text parser produced rows on). Bounded by ``max_pages``.
    Returns ``(entries, stats)`` with ``stats = {pagesRead, calls}``.
    """
    import tempfile

    if not (base_url and api_key and model) or not candidate_pages:
        return [], {"pagesRead": 0, "calls": 0}
    pages = sorted({p for p in candidate_pages if p and p > 0})[: max_pages or len(candidate_pages)]
    tmp = work_dir or Path(tempfile.mkdtemp(prefix="vision-parts-"))
    tmp.mkdir(parents=True, exist_ok=True)

    entries: list[PartsListEntry] = []
    seq = 0
    calls = 0
    for page in pages:
        png = render_figure_png(pdf_path, page, tmp / f"page-{page}", dpi=dpi)
        if png is None:
            continue
        calls += 1
        for row in _vision_rows(png, base_url=base_url, api_key=api_key, model=model):
            entry = _to_entry(row, sequence=seq, page_number=page)
            if entry is not None:
                entries.append(entry)
                seq += 1
    return entries, {"pagesRead": len(pages), "calls": calls}
