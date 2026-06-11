"""SPICE model resolution for extracted components (catalog-driven, generic).

A drawing can tell us a part's printed number, never its electrical model —
inventing one would be fabricated physics. Instead, models come from a
MODEL LIBRARY DIRECTORY (``DOCLING_SERVE_SPICE_MODEL_DIR``): one ``.lib`` /
``.sub`` / ``.mod`` file per part, named by the part's normalized number
(``KIDDE_870929.lib``). Engineers — or a sync job from the ontology's Part
Catalog — drop vendor models there and every subsequent extraction binds
them automatically:

* the ``.cir`` netlist inlines the model and instances reference its real
  ``.subckt``/``.model`` name instead of a stub,
* KiCad symbol instances get ``Sim.*`` properties so KiCad's simulator
  binds the same model.

Nothing is keyed to any specific drawing or vendor; resolution is purely
``normalize(partNumber) -> file``.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path

_log = logging.getLogger(__name__)

#: Recognized model library file suffixes, in preference order.
MODEL_SUFFIXES = (".lib", ".sub", ".mod", ".cir")

# Horizontal whitespace only: with MULTILINE, \s would run across newlines
# and swallow the subcircuit BODY into the pin list.
_SUBCKT_RE = re.compile(
    r"^[ \t]*\.subckt[ \t]+(\S+)((?:[ \t]+\S+)*)[ \t]*$", re.IGNORECASE | re.MULTILINE
)
_MODEL_RE = re.compile(r"^\s*\.model\s+(\S+)\s+(\S+)", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class SpiceModel:
    """One resolved vendor model: its source text and entry point."""

    part_number: str
    path: Path
    text: str
    #: ``.subckt`` name (preferred) or ``.model`` name.
    name: str
    #: Pin count for subcircuits; 2 for primitive ``.model`` devices.
    pin_count: int
    #: True when the entry point is a ``.subckt`` (instances use ``X``).
    is_subckt: bool
    #: SPICE device type token of a ``.model`` entry (``PMOS``, ``NPN``, ``D``,
    #: …); empty for subcircuits.
    model_type: str = ""


def normalize_part_number(part_number: str) -> str:
    """Library key: alphanumerics and underscores, upper-cased."""
    return re.sub(r"[^A-Za-z0-9]+", "_", part_number.strip()).strip("_").upper()


def model_library_dir() -> Path | None:
    """The configured model library directory, if it exists."""
    configured = os.environ.get("DOCLING_SERVE_SPICE_MODEL_DIR", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_dir() else None
    return None


def find_model(
    part_number: str | None,
    *,
    library_dir: Path | None = None,
    tenant_id: str | None = None,
) -> SpiceModel | None:
    """Resolve a part's vendor model from the library directory, if any.

    Tenant-scoped models (``<dir>/tenants/<tenant>/<KEY>.lib`` — what the
    pytology catalog sync publishes) take precedence over shared root-level
    models, and one tenant's models can never bind another tenant's parts.
    """
    if not part_number:
        return None
    directory = library_dir if library_dir is not None else model_library_dir()
    if directory is None:
        return None
    key = normalize_part_number(part_number)
    if not key:
        return None
    search_dirs = (
        [directory / "tenants" / tenant_id, directory] if tenant_id else [directory]
    )
    candidates = [
        base / f"{key}{suffix}" for base in search_dirs for suffix in MODEL_SUFFIXES
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            text = path.read_text()
        except OSError as error:  # pragma: no cover - filesystem dependent
            _log.warning("Unreadable SPICE model %s: %s", path, error)
            return None
        parsed = _entry_point(text)
        if parsed is None:
            _log.warning("SPICE model %s has no .subckt/.model entry point", path)
            return None
        name, pin_count, is_subckt, model_type = parsed
        return SpiceModel(
            part_number=part_number,
            path=path,
            text=text.strip(),
            name=name,
            pin_count=pin_count,
            is_subckt=is_subckt,
            model_type=model_type,
        )
    return None


def _entry_point(text: str) -> tuple[str, int, bool, str] | None:
    subckt = _SUBCKT_RE.search(text)
    if subckt:
        pins = [p for p in subckt.group(2).split() if "=" not in p]
        return subckt.group(1), len(pins), True, ""
    model = _MODEL_RE.search(text)
    if model:
        return model.group(1), 2, False, model.group(2).split("(")[0].upper()
    return None
