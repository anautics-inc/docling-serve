"""Generate a synthetic harness-drawing fixture with KNOWN ground truth.

Real military/harness drawings print part numbers, install locations, and wire
ids — exactly the fields the schematic extractor's enriched prompt asks for —
but our hobbyist test schematic has none. This generator writes a small,
EESV-style wiring diagram as a raw vector PDF (no dependencies: hand-built
objects, Helvetica text, zlib-compressed content stream so the ``profile=auto``
router's vector-op heuristic sees it) plus the matching ground-truth JSON, so:

1. the partNumber → ontology catalog match can be exercised end to end
   (part numbers are chosen from the live ``Part Catalog Item`` rows), and
2. extraction accuracy (component recall/precision, net membership) can be
   MEASURED against truth instead of eyeballed — see
   ``scripts/schematic_accuracy.py``.

Usage:
    python scripts/make_harness_fixture.py [out_dir]

Writes ``harness_fixture.pdf`` + ``harness_fixture.truth.json``.
"""

from __future__ import annotations

import json
import sys
import zlib
from pathlib import Path

PAGE_W, PAGE_H = 1000, 700

GROUND_TRUTH = {
    "components": [
        {"refDes": "V1", "type": "valve", "partNumber": "BDP-000002", "location": "RH SIDE STA 21"},
        {"refDes": "K1", "type": "relay", "partNumber": "BDP-000003", "location": "E-BAY SHELF 2"},
        {"refDes": "U1", "type": "ECU", "partNumber": "BDP-000005", "location": "FWD AVIONICS"},
        {"refDes": "E1", "type": "ground stud", "partNumber": None, "location": None},
    ],
    "nets": [
        {"name": "A8B22", "members": ["V1", "K1"]},
        {"name": "A9A20", "members": ["K1", "U1"]},
        {"name": "GND", "members": ["V1", "U1", "E1"]},
    ],
}


class _Content:
    """PDF content-stream builder (y-up PDF space; callers pass y-down coords)."""

    def __init__(self) -> None:
        self.ops: list[str] = []

    @staticmethod
    def _y(y: float) -> float:
        return PAGE_H - y

    def line(self, x1: float, y1: float, x2: float, y2: float, width: float = 1.0) -> None:
        self.ops.append(f"{width} w {x1} {self._y(y1)} m {x2} {self._y(y2)} l S")

    def polyline(self, points: list[tuple[float, float]], width: float = 1.0) -> None:
        x0, y0 = points[0]
        segs = " ".join(f"{x} {self._y(y)} l" for x, y in points[1:])
        self.ops.append(f"{width} w {x0} {self._y(y0)} m {segs} S")

    def rect(self, x: float, y: float, w: float, h: float, width: float = 1.2) -> None:
        self.ops.append(f"{width} w {x} {self._y(y + h)} {w} {h} re S")

    def text(self, x: float, y: float, content: str, size: float = 11) -> None:
        escaped = content.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        self.ops.append(f"BT /F1 {size} Tf {x} {self._y(y)} Td ({escaped}) Tj ET")

    def box_with_label(self, x: float, y: float, w: float, h: float, lines: list[str]) -> None:
        self.rect(x, y, w, h)
        for index, line in enumerate(lines):
            self.text(x + 8, y + 18 + index * 14, line)

    def build(self) -> bytes:
        return "\n".join(self.ops).encode("ascii")


def build_content() -> bytes:
    c = _Content()
    # Drawing border + ruler ticks (real-drawing frame; also pushes the vector
    # line-op count over the profile=auto router's threshold).
    c.rect(20, 20, PAGE_W - 40, PAGE_H - 40, width=1.5)
    for i in range(80):
        x = 35 + i * 11.6
        c.line(x, 20, x, 28, width=0.6)
        c.line(x, PAGE_H - 28, x, PAGE_H - 20, width=0.6)
    for i in range(56):
        y = 35 + i * 11.4
        c.line(20, y, 28, y, width=0.6)
        c.line(PAGE_W - 28, y, PAGE_W - 20, y, width=0.6)

    # Components.
    c.box_with_label(100, 150, 190, 110, ["V1  GUN CHARGING VALVE", "P/N BDP-000002", "RH SIDE STA 21", "PIN A   PIN B"])
    c.box_with_label(450, 150, 170, 110, ["K1  CONTROL RELAY", "P/N BDP-000003", "E-BAY SHELF 2", "PIN 1   PIN 2"])
    c.box_with_label(760, 150, 170, 110, ["U1  ECU MAIN", "P/N BDP-000005", "FWD AVIONICS", "PIN J1-1  J1-2"])
    # Ground stud E1 (classic 3-bar ground symbol + label).
    c.line(180, 470, 180, 500)
    c.line(160, 500, 200, 500, width=1.4)
    c.line(167, 507, 193, 507, width=1.2)
    c.line(174, 514, 186, 514)
    c.text(208, 505, "E1 GROUND STUD")

    # Wires (touch the component boxes only at their edges).
    c.line(290, 190, 450, 190, width=0.9)
    c.text(330, 182, "A8B22  22 AWG", size=10)
    c.line(620, 190, 760, 190, width=0.9)
    c.text(655, 182, "A9A20  20 AWG", size=10)
    # GND bus: V1 bottom and U1 bottom down to E1.
    c.line(180, 260, 180, 470, width=0.9)
    c.text(188, 380, "GND", size=10)
    c.polyline([(845, 260), (845, 430), (180, 430)], width=0.9)
    c.text(500, 422, "GND", size=10)

    # Title block.
    c.box_with_label(620, 560, 310, 90, ["TITLE: HARNESS FIXTURE", "DWG NO: HFX-001   REV A", "SHEET 1/1   2026-06-09"])
    return c.build()


def build_pdf() -> bytes:
    stream = zlib.compress(build_content())
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            "/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>"
        ).encode("ascii"),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        (f"<< /Length {len(stream)} /Filter /FlateDecode >>".encode("ascii"), stream),
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode("ascii")
        if isinstance(obj, tuple):
            head, body = obj
            out += head + b"\nstream\n" + body + b"\nendstream\nendobj\n"
        else:
            out += obj + b"\nendobj\n"
    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii")
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode("ascii")
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n"
    ).encode("ascii")
    return bytes(out)


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tests/test_files")
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = out_dir / "harness_fixture.pdf"
    truth_path = out_dir / "harness_fixture.truth.json"
    pdf_path.write_bytes(build_pdf())
    truth_path.write_text(json.dumps(GROUND_TRUTH, indent=2))
    print(f"wrote {pdf_path} and {truth_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
