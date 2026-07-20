"""Deterministic non-schematic content guard.

Some engineering drawings that reach this extractor are not electrical
schematics at all — an exploded mechanical parts view (the "Exploded View" /
"Parts Breakdown" figures in an illustrated parts breakdown) shares the same
PDF/raster/vector container, but its index callouts identify PARTS, not net
topology. Sending that content through the schematic model prompt ("read
this drawing as components + nets, return a netlist") produces a graph that
LOOKS authoritative — refDes-shaped labels, wires, a KiCad file, a SPICE deck
— but is fabricated relative to the source: there is no circuit to trace.

This module is a cheap, deterministic pre-flight check — no model call — so
the extractor can refuse a specific page instead of publishing a misleading
schematic graph for it. It is intentionally conservative: ambiguous pages
fall through to the model rather than being blocked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pikepdf

#: Caption/body vocabulary that identifies a mechanical exploded/parts view.
_EXPLODED_VIEW_RE = re.compile(
    r"\bEXPLODED\s+VIEW\b|\bPARTS?\s+BREAKDOWN\b|\bILLUSTRATED\s+PARTS\b",
    re.I,
)
#: Circuit/schematic vocabulary — a strong positive signal that overrides the
#: exploded-view guard even on a raster (scanned) page.
_SCHEMATIC_VOCAB_RE = re.compile(
    r"\bSCHEMATIC\b|\bWIRING\s+DIAGRAM\b|\bCIRCUIT\s+DIAGRAM\b|\bBLOCK\s+DIAGRAM\b"
    r"|\bLOGIC\s+DIAGRAM\b",
    re.I,
)
#: Reference-designator-shaped tokens (R1, C12, U3A, TB2, J4) — the
#: vocabulary of an actual circuit, essentially absent from a mechanical
#: exploded view whose callouts are bare index numbers.
_REFDES_TOKEN_RE = re.compile(r"\b[RCLUQDJKTMSY]\d{1,4}[A-Z]?\b")


@dataclass(slots=True)
class DrawingContentVerdict:
    is_non_schematic: bool
    reason: str = ""
    signals: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "isNonSchematic": self.is_non_schematic,
            "reason": self.reason,
            "signals": self.signals,
        }


def has_raster_image(pdf_path: Path) -> bool:
    """True when any page resource contains an image XObject."""
    try:
        with pikepdf.Pdf.open(pdf_path) as pdf:
            for page in pdf.pages:
                # pikepdf's generic Object stub cannot express PDF dictionary
                # value types, so narrow only this third-party boundary.
                resources = cast(dict[str, Any], page.obj.get("/Resources", {}))
                xobjects = cast(
                    dict[str, Any],
                    resources.get("/XObject", {}) if resources else {},
                )
                for value in xobjects.values():
                    if value.get("/Subtype") == pikepdf.Name("/Image"):
                        return True
    except (OSError, pikepdf.PdfError, AttributeError, TypeError):
        return False
    return False


def classify_drawing_content(
    *, raster_backed: bool, page_text: str
) -> DrawingContentVerdict:
    """Best-effort, conservative refusal signal for non-schematic art.

    Only flags ``is_non_schematic=True`` when the page is raster-backed (a
    real vector schematic is never a photo/scan) AND its caption/body text
    uses exploded-view/parts-breakdown language AND shows no schematic
    vocabulary or refDes-shaped tokens. Any ambiguity resolves to "let the
    model try" (``is_non_schematic=False``) — this guard exists to catch the
    clear case, not to gate every drawing.
    """
    signals: list[str] = []
    if not raster_backed:
        return DrawingContentVerdict(is_non_schematic=False)
    signals.append("raster-backed page")

    exploded_hit = _EXPLODED_VIEW_RE.search(page_text)
    if not exploded_hit:
        return DrawingContentVerdict(is_non_schematic=False)
    signals.append(f"caption/body text: {exploded_hit.group(0)!r}")

    if schematic_hit := _SCHEMATIC_VOCAB_RE.search(page_text):
        signals.append(f"schematic vocabulary present: {schematic_hit.group(0)!r}")
        return DrawingContentVerdict(is_non_schematic=False, signals=signals)
    if refdes_hit := _REFDES_TOKEN_RE.search(page_text):
        signals.append(f"refDes-shaped token present: {refdes_hit.group(0)!r}")
        return DrawingContentVerdict(is_non_schematic=False, signals=signals)

    return DrawingContentVerdict(
        is_non_schematic=True,
        reason=(
            "page is a raster exploded-view / parts-breakdown illustration, "
            "not an electrical schematic"
        ),
        signals=signals,
    )
