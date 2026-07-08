"""Central, env-overridable tuning for the schematic pipeline.

Every heuristic threshold the extraction depends on lived as a module-level
magic number tuned by hand against a couple of drawings. That is the opposite
of production-ready: the values can't be adjusted per deployment, can't be
swept by the eval harness, and hide the fact that they ARE assumptions.

This collects them into one dataclass with an ``from_env`` loader so a
deployment (or the eval sweep) can override any of them via
``SCHEMATIC_<FIELD>`` environment variables without a code change. Defaults
are the reviewed values; each carries a one-line rationale.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class SchematicTuning:
    # --- net tracing -------------------------------------------------------
    #: A traced segment hugging a large component's bbox edge within this many
    #: pt is that component's drawn enclosure outline, not a wire.
    outline_hug_pt: float = 3.0
    #: Only boxes at least this big (pt) have meaningful drawn enclosures.
    outline_min_box_pt: float = 90.0

    # --- component reconciliation ------------------------------------------
    #: Two boxes overlapping by at least this IoU are one physical glyph seen
    #: by both recognition passes (overlap, not containment, so a small symbol
    #: inside a large enclosure box is never absorbed).
    duplicate_merge_iou: float = 0.45
    #: A net segment endpoint within this many pt of a component bbox counts
    #: as terminating at it (retroactive touch for missed clips).
    bbox_attach_tolerance_pt: float = 6.0

    # --- glyph disambiguation ---------------------------------------------
    #: Minimum crop-recheck confidence to ACT on a verdict (reclassify/drop).
    glyph_verdict_confidence: float = 0.6
    #: When true, a value-less capacitor is KEPT only if the crop check
    #: positively confirms it as a capacitor; any other verdict (ground,
    #: winding, other, low confidence) drops or retypes it. This flips the
    #: burden of proof toward the reviewed stance: an invented capacitor
    #: poisons downstream artifacts, an omitted one is recoverable.
    glyph_confirm_to_keep: bool = True

    # --- confidence gate ---------------------------------------------------
    #: Extractions whose evidence-backed component fraction is below this are
    #: flagged needsReview (not silently published as trustworthy).
    min_verified_fraction_gate: float = 0.6

    # --- simulation defaults ----------------------------------------------
    #: Default rail voltage by circuit kind when a supply net names no value.
    default_volts_electromechanical: float = 28.0  # MIL-STD-704 28 V DC bus
    default_volts_digital: float = 5.0
    default_volts_analog: float = 5.0

    @classmethod
    def from_env(cls) -> "SchematicTuning":
        """Load defaults, overriding any field from ``SCHEMATIC_<FIELD>``."""
        overrides: dict[str, object] = {}
        for field in fields(cls):
            env_key = f"SCHEMATIC_{field.name.upper()}"
            raw = os.environ.get(env_key)
            if raw is None:
                continue
            if field.type == "bool":
                overrides[field.name] = raw.strip().lower() in ("1", "true", "yes")
            else:
                try:
                    overrides[field.name] = float(raw)
                except ValueError:
                    continue
        return cls(**overrides)  # type: ignore[arg-type]


#: Process-wide singleton, resolved once from the environment.
TUNING = SchematicTuning.from_env()
