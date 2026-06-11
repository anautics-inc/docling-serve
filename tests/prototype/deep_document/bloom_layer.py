"""Bloom-taxonomy classification over the Docling-centric spine.

Adapts the spine block shape (`kind` ∈ text|table|picture) to the semantic
provider abstraction from `semantics.py`:

  - text  → classified on its text
  - table → classified on joined cell text (provider routes to a table rule)
  - picture → classified on its vision caption when present; otherwise an
              explicit opaque-structural tag (never a fabricated level)
  - notes → classified on cleaned speaker notes
  - unit  → aggregated from its blocks + notes

Default provider is the deterministic keyword fallback; Bedrock is selected
via `CAPTIFY_DEEP_DOC_SEMANTIC_PROVIDER=bedrock`. No model id, threshold, or
provider choice is hardcoded.
"""
from __future__ import annotations

from typing import Any

from . import bloom_classifier as bloom
from .semantics import DecisionContext, provider_from_environment


def _table_text(block: dict[str, Any]) -> str:
    cells = (block.get("table") or {}).get("cells") or []
    return " ".join(str(cell.get("text", "")) for cell in cells).strip()


def apply_bloom(manifest: dict[str, Any], provider: Any | None = None) -> dict[str, Any]:
    """Classify every spine block / notes / unit and attach `manifest['taxonomy']`.

    Mutates the manifest in place. Returns the taxonomy summary dict (also
    stored on the manifest). Picture blocks without a caption are tagged
    opaque rather than guessed.
    """
    provider = provider or provider_from_environment()
    units = manifest.get("units", [])
    total = len(units)

    # AUDIT F1 fix: vision captions are written to the asset table by
    # `caption_assets`, but picture blocks only carry an `assetId`. Resolve
    # captions through `assetId` so a captioned picture is classified on its
    # caption — not left opaque.
    caption_by_asset: dict[str, str] = {}
    for asset in manifest.get("assets", []):
        caption_text = (asset.get("caption") or {}).get("text")
        if asset.get("assetId") and caption_text:
            caption_by_asset[asset["assetId"]] = caption_text

    contexts: list[DecisionContext] = []
    block_targets: dict[str, dict[str, Any]] = {}
    notes_targets: dict[str, dict[str, Any]] = {}

    for unit in units:
        for block in unit["blocks"]:
            kind = block["kind"]
            if kind == "picture":
                # Caption may live on the block (legacy) or on the linked asset.
                caption = (block.get("caption") or {}).get("text")
                if not caption:
                    caption = caption_by_asset.get(block.get("assetId") or "")
                if not caption:
                    # No vision caption anywhere — explicit opaque tag, not a guess.
                    block["classification"] = bloom.classify_opaque_structural("picture")
                    continue
                text = caption
            elif kind == "table":
                text = _table_text(block)
            else:
                text = block.get("text") or ""

            context = DecisionContext(
                target_type="block",
                target_id=block["blockId"],
                unit_id=unit["unitId"],
                text=text,
                kind=kind,
            )
            contexts.append(context)
            block_targets[block["blockId"]] = block

        notes = unit.get("speakerNotes") or {}
        cleaned = notes.get("cleaned")
        if cleaned:
            target_id = f"{unit['unitId']}:speakerNotes"
            contexts.append(
                DecisionContext(
                    target_type="notes",
                    target_id=target_id,
                    unit_id=unit["unitId"],
                    text=cleaned,
                    kind="speaker_notes",
                )
            )
            notes_targets[target_id] = notes
        else:
            notes["classification"] = None

    results = provider.classify_many(contexts) if contexts else {}

    for block_id, block in block_targets.items():
        block["classification"] = results.get(block_id) or bloom.classify(
            block.get("text") or "", source="block_text"
        )
    for target_id, notes in notes_targets.items():
        notes["classification"] = results.get(target_id) or bloom.classify(
            notes.get("cleaned") or "", source="speaker_notes"
        )

    # Aggregate each unit from its blocks + notes.
    for unit in units:
        child = [b["classification"] for b in unit["blocks"]]
        notes_classification = (unit.get("speakerNotes") or {}).get("classification")
        unit["classification"] = bloom.aggregate_classification(
            [*child, notes_classification],
            unit.get("title") or "",
            source="aggregate",
            index=unit["index"],
            total=total,
        )

    # Assets are classified via their picture blocks, not separately — pass [].
    taxonomy = bloom.taxonomy_summary(units, [])
    taxonomy["provider"] = provider.provider_id
    manifest["taxonomy"] = taxonomy
    return taxonomy
