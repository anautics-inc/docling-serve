"""Crop-verification of component labels (refDes / part number).

Whole-page reading binds parts to the wrong reference designators on dense
drawings — the measured #1 accuracy gap (Arduino UNO: ATMEGA328P filed under
"Z14" instead of ZU4; one part number substituted from prior knowledge instead
of the print). The cure is focus: re-read each significant component's labels
from a CROP of just that component, where there is nothing nearby to confuse
and nothing to "know".

The pass sends ONE small crop per model call (grid sheets were tried first
and the model misaligned cell indexes, binding labels to the wrong crops —
the very bug this pass exists to fix), asks for verbatim transcription only,
and corrects the main result in place — including rewriting the model nets'
refDes references so downstream tracing and naming stay consistent. Only
significant components (ICs, connectors, anything carrying a part number)
are verified, and every response is content-hash cached, so the steady-state
cost is zero and the first-run cost is bounded by MAX_VERIFY_COMPONENTS
small-image calls.

Correction semantics (measured on the Arduino UNO sheet): the main pass's
bounding boxes are sloppy enough that a crop often shows a NEIGHBOURING
component, so corrections are keyed by the refDes *transcribed in the crop*,
not by which component the crop was cut for. A printed refDes and part
number are co-located on the drawing, so the pair is trustworthy even when
the crop identity is not: if a component with that refDes exists anywhere on
the page, the verified part number overrides its binding; only when the
refDes is unclaimed (and the pair is complete) is the cropped component
renamed. Transcriptions without a plausible designator ("AREF", "100u",
"470 100") are discarded, and part numbers are only overridden, never
nulled, by this pass.
"""

from __future__ import annotations

import io
import logging
import re
from typing import Any

_log = logging.getLogger(__name__)

VERIFY_SYSTEM_PROMPT = (
    "You are a transcription engine for engineering-drawing crops. You copy "
    "printed text EXACTLY as it appears — never inferring, completing, or "
    "substituting from knowledge of electronic devices. You always answer "
    "with a single valid JSON object and nothing else."
)

VERIFY_USER_PROMPT = (
    "This image is a crop showing ONE component from an engineering "
    "schematic. Transcribe the component's printed labels.\n"
    "Return ONLY a JSON object:\n"
    '{"refDes": str|null, "partNumber": str|null}\n'
    "Rules:\n"
    "- refDes is the reference designator printed by the symbol (e.g. R1, ZU4, "
    "U5A, TB1). partNumber is the manufacturer part number printed by the "
    "symbol (e.g. NCP1117ST50T3G, KIDDE 870929) — usually the longest "
    "alphanumeric string by the symbol; NOT a value like 100n, 10k, 22R.\n"
    "- TRANSCRIBE ONLY what is fully visible in the crop. If a label is "
    "absent, cut off, or partially illegible, use null for it. Never guess "
    "a part number from what the device looks like.\n"
    "- Reference designators always BEGIN WITH LETTERS (R1, ZU4, TB12); "
    "read the leading characters carefully.\n"
    "- Text belonging to NEIGHBOURING components may intrude at the crop "
    "edges; ignore anything not attached to the central component."
)

#: At most this many components are verified per page (one small model call
#: each, content-hash cached). Only significant components qualify.
MAX_VERIFY_COMPONENTS = 16
_MAX_CROP_SIZE = 640
_CROP_PAD_FRAC = 0.25

#: Component types that matter most for the digital twin (verified first).
_SIGNIFICANT_TYPES = (
    "ic", "connector", "relay", "regulator", "op-amp", "opamp", "mcu",
    "microcontroller", "transistor", "valve", "ecu", "switch", "crystal",
    "fuse", "diode", "display", "tube",
)


def _is_significant(component: dict[str, Any]) -> bool:
    if component.get("partNumber"):
        return True
    ctype = str(component.get("type") or "").lower()
    return any(token in ctype for token in _SIGNIFICANT_TYPES)


def select_components(components: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Boxed significant components to verify, capped."""
    boxed = [
        c
        for c in components
        if isinstance(c, dict) and c.get("bbox") and _is_significant(c)
    ]
    return boxed[:MAX_VERIFY_COMPONENTS]


def _component_crops(
    page_png: bytes,
    components: list[dict[str, Any]],
    image_size: dict[str, Any],
) -> list[tuple[int, bytes]]:
    """Crop each component (padded) from the full-resolution page render."""
    from PIL import Image

    page = Image.open(io.BytesIO(page_png)).convert("RGB")
    try:
        model_w = float(image_size.get("w") or 0)
        model_h = float(image_size.get("h") or 0)
    except (TypeError, ValueError):
        return []
    if model_w <= 0 or model_h <= 0:
        return []
    sx, sy = page.width / model_w, page.height / model_h

    crops: list[tuple[int, bytes]] = []
    for index, component in enumerate(components):
        x0, y0, x1, y1 = (float(v) for v in component["bbox"])
        pad_x = (x1 - x0) * _CROP_PAD_FRAC
        pad_y = (y1 - y0) * _CROP_PAD_FRAC
        box = (
            max(0, int((x0 - pad_x) * sx)),
            max(0, int((y0 - pad_y) * sy)),
            min(page.width, int((x1 + pad_x) * sx)),
            min(page.height, int((y1 + pad_y) * sy)),
        )
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            continue
        crop = page.crop(box)
        if crop.width > _MAX_CROP_SIZE or crop.height > _MAX_CROP_SIZE:
            scale = min(_MAX_CROP_SIZE / crop.width, _MAX_CROP_SIZE / crop.height)
            crop = crop.resize((int(crop.width * scale), int(crop.height * scale)))
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG", optimize=True)
        crops.append((index, buffer.getvalue()))
    return crops


def apply_corrections(
    result: dict[str, Any],
    verified: dict[int, dict[str, Any]],
    selected: list[dict[str, Any]],
) -> int:
    """Fold verified (refDes, partNumber) pairs into the page result, in place.

    Pairs are keyed by the refDes transcribed in the crop, not by which
    component the crop was cut for (boxes are sloppy). For each complete
    pair: a unique part-number match identifies the component that should
    carry that designator (rename, applied as a simultaneous permutation
    with displacement swaps); otherwise an existing component with that
    designator gets its part number overridden. Returns corrections applied.
    """
    pairs = _collect_pairs(verified)
    if not pairs:
        return 0
    components = [c for c in result.get("components") or [] if isinstance(c, dict)]
    by_ref = {str(c["refDes"]).upper(): c for c in components if c.get("refDes")}
    by_part: dict[str, list[dict[str, Any]]] = {}
    for component in components:
        normalized = _normalize_part(component.get("partNumber"))
        if normalized:
            by_part.setdefault(normalized, []).append(component)

    alias, corrections = _apply_renames(components, pairs, by_ref, by_part)
    corrections += _apply_part_overrides(pairs, by_ref, by_part)
    adoption_alias, adopted = _apply_crop_adoptions(
        verified, selected, pairs, by_ref, by_part
    )
    alias.update(adoption_alias)
    corrections += adopted
    if alias:
        _rewrite_refdes_references(result, alias)
    return corrections


def _apply_crop_adoptions(
    verified: dict[int, dict[str, Any]],
    selected: list[dict[str, Any]],
    pairs: dict[str, tuple[str, str | None]],
    by_ref: dict[str, dict[str, Any]],
    by_part: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, str], int]:
    """The cropped component adopts a verified pair that matches NOTHING.

    On dense sheets the whole-page pass invents designators outright (Q1-Q6
    where the print says SW1-SW6) — then a complete transcribed pair has no
    refDes to override and no part number to match. The crop was cut at that
    component's location, so the strongest remaining evidence is that THIS
    component is the one printed with the pair: rename it and take the part
    number.
    """
    alias: dict[str, str] = {}
    corrections = 0
    claimed = set(by_ref)
    for index, cell in sorted(verified.items()):
        if index >= len(selected) or not isinstance(cell, dict):
            continue
        ref = _text(cell.get("refDes"))
        key = ref.upper() if ref else None
        if not key or key not in pairs:
            continue
        ref_raw, part = pairs[key]
        if not part:
            continue  # incomplete pair: not enough evidence to re-identify
        if key in claimed or by_part.get(_normalize_part(part) or ""):
            continue  # already handled by rename/override passes
        component = selected[index]
        if component.get("refDesVerified") or component.get("partNumberVerified"):
            continue
        old_ref = _text(component.get("refDes"))
        if old_ref:
            alias[old_ref.upper()] = ref_raw
        _log.info(
            "label-verify crop adoption: %r -> %r (%r)", old_ref, ref_raw, part
        )
        component["refDes"] = ref_raw
        component["partNumber"] = part
        component["refDesVerified"] = True
        component["partNumberVerified"] = True
        claimed.add(key)
        corrections += 1
    return alias, corrections


def _collect_pairs(
    verified: dict[int, dict[str, Any]],
) -> dict[str, tuple[str, str | None]]:
    """Validated transcription pairs keyed by upper-cased refDes.

    Two crops claiming the same designator with different part numbers
    cancel each other out.
    """
    pairs: dict[str, tuple[str, str | None]] = {}
    conflicted: set[str] = set()
    for cell in verified.values():
        if not isinstance(cell, dict):
            continue
        ref = _text(cell.get("refDes"))
        if not ref or not _plausible_refdes(ref):
            continue
        part = _text(cell.get("partNumber"))
        if part and not _plausible_part_number(part):
            part = None
        key = ref.upper()
        if key in pairs and pairs[key][1] != part:
            conflicted.add(key)
        pairs.setdefault(key, (ref, part))
    for key in conflicted:
        pairs.pop(key, None)
    return pairs


def _apply_renames(
    components: list[dict[str, Any]],
    pairs: dict[str, tuple[str, str | None]],
    by_ref: dict[str, dict[str, Any]],
    by_part: dict[str, list[dict[str, Any]]],
) -> tuple[dict[str, str], int]:
    """Rename components identified by a unique part-number match.

    Renames apply as a simultaneous permutation: an occupant of a target
    designator that is itself being renamed is no obstacle, and an
    UNVERIFIED occupant is displaced into the renamed component's old
    designator (the verified pair beats the whole-page binding).
    """
    final: dict[int, str] = {}
    renamed: dict[int, dict[str, Any]] = {}
    for key, (ref_raw, part) in pairs.items():
        if not part:
            continue
        matches = by_part.get(_normalize_part(part) or "") or []
        if len(matches) != 1:
            continue
        component = matches[0]
        current = str(component.get("refDes") or "").upper()
        if current == key or _base_refdes(current) == _base_refdes(key):
            continue
        final[id(component)] = ref_raw
        renamed[id(component)] = component

    # Displace unverified occupants into the vacated designators.
    for component_id, new_ref in list(final.items()):
        occupant = by_ref.get(new_ref.upper())
        if occupant is not None and id(occupant) not in final:
            old_ref = renamed[component_id].get("refDes")
            if old_ref:
                final[id(occupant)] = str(old_ref)
                renamed[id(occupant)] = occupant

    # Drop renames that would produce duplicate designators.
    resulting: dict[str, int] = {}
    for component in components:
        name = final.get(id(component)) or component.get("refDes")
        if name:
            key = str(name).upper()
            resulting[key] = resulting.get(key, 0) + 1
    for component_id, new_ref in list(final.items()):
        if resulting.get(new_ref.upper(), 0) > 1:
            final.pop(component_id)
            renamed.pop(component_id, None)

    alias: dict[str, str] = {}
    corrections = 0
    for component_id, new_ref in final.items():
        component = renamed[component_id]
        old_ref = _text(component.get("refDes"))
        if old_ref:
            alias[old_ref.upper()] = new_ref
        _log.info("label-verify rename: %r -> %r", old_ref, new_ref)
        component["refDes"] = new_ref
        component["refDesVerified"] = True
        corrections += 1
    return alias, corrections


def _apply_part_overrides(
    pairs: dict[str, tuple[str, str | None]],
    by_ref: dict[str, dict[str, Any]],
    by_part: dict[str, list[dict[str, Any]]],
) -> int:
    """Override part numbers on designator matches without a pn-keyed rename."""
    corrections = 0
    for key, (_ref_raw, part) in pairs.items():
        if not part or by_part.get(_normalize_part(part) or ""):
            continue
        component = by_ref.get(key)
        if component is None or _text(component.get("partNumber")) == part:
            continue
        _log.info(
            "label-verify part override on %r: %r -> %r",
            component.get("refDes"), component.get("partNumber"), part,
        )
        component["partNumber"] = part
        component["partNumberVerified"] = True
        corrections += 1
    return corrections


def _rewrite_refdes_references(result: dict[str, Any], alias: dict[str, str]) -> None:
    """Apply a refDes rename map to net nodes and connector parents."""
    for net in result.get("nets") or []:
        if not isinstance(net, dict):
            continue
        for node in net.get("nodes") or []:
            if isinstance(node, dict) and node.get("refDes"):
                replacement = alias.get(str(node["refDes"]).upper())
                if replacement:
                    node["refDes"] = replacement
    for component in result.get("components") or []:
        if isinstance(component, dict) and component.get("parentComponent"):
            replacement = alias.get(str(component["parentComponent"]).upper())
            if replacement:
                component["parentComponent"] = replacement


def verify_component_labels(
    result: dict[str, Any],
    page_png: bytes,
    *,
    understand: Any,
) -> int:
    """Run the crop-verification pass over one page result, in place.

    ``understand(prompt, system, png_bytes) -> dict`` abstracts the (cached)
    model call. Never raises; returns the number of corrections applied.
    """
    components = [c for c in result.get("components") or [] if isinstance(c, dict)]
    selected = select_components(components)
    if not selected:
        return 0
    try:
        crops = _component_crops(page_png, selected, result.get("imageSize") or {})
    except Exception as error:
        _log.warning("Verification crop build failed: %s", error)
        return 0

    verified: dict[int, dict[str, Any]] = {}
    for index, crop_png in crops:
        try:
            payload = understand(VERIFY_USER_PROMPT, VERIFY_SYSTEM_PROMPT, crop_png)
        except Exception as error:
            _log.warning("Verification model call failed: %s", error)
            continue
        if isinstance(payload, dict):
            verified[index] = payload
    if not verified:
        return 0
    return apply_corrections(result, verified, selected)


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


#: Designator = letters then a number, optionally a gate suffix (R1, ZU4,
#: IC2, RN2B, TB12). Filters out net labels ("AREF"), values ("100u"), and
#: pin names ("D+") that the model mistakes for designators in tight crops.
_REFDES_RE = re.compile(r"^[A-Z]{1,4}[0-9]{1,4}[A-Z]?$")


def _plausible_refdes(text: str) -> bool:
    return bool(_REFDES_RE.match(text.upper()))


def _base_refdes(text: str) -> str:
    """Designator without a trailing gate suffix: U5A -> U5."""
    return re.sub(r"(?<=[0-9])[A-Z]$", "", text.upper())


def _normalize_part(value: Any) -> str | None:
    """Comparison key for part numbers: alphanumerics only, upper-cased."""
    if value is None:
        return None
    normalized = re.sub(r"[^A-Z0-9]", "", str(value).upper())
    return normalized or None


def _plausible_part_number(text: str) -> bool:
    """Part numbers are long-ish and not bare values like '100u' or '10k'."""
    if len(text) < 5 or _REFDES_RE.match(text.upper()):
        return False
    return bool(re.search(r"[0-9]", text)) and not re.match(
        r"^[0-9.]+\s*[A-Za-zµΩ%]{0,3}$", text
    )
