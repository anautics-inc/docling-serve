# Experiment 6: Docling-Centric Re-Architecture

**Date:** 2026-05-20
**Parent:** experiments 2–5 in this folder.
**Status:** in progress — driven by a self-paced `/loop`.

---

## Why this exists

Experiments 2–5 drifted: OOXML parsing became the whole extraction engine and Docling became an unused sidecar. That is a dead end — an OOXML-centric pipeline cannot extract a PDF, and the product needs PPT + Word + PDF + Excel through one pipeline.

Experiment6 puts Docling back at the center. The `DoclingDocument` JSON is the structural spine for **every** format; OOXML is demoted to a narrow PPTX-only enrichment for the four things Docling's PPTX backend documents that it skips.

## Hard rules

1. **No hardcoding.** Every path, threshold, model ID, grid dimension, format assumption is a parameter or config value. The test for every line: would this run unmodified on PPT #438 nobody has seen? Hundreds of decks will be uploaded.
2. **Docling is the spine.** `units`/`blocks`/`tables`/`pictures` are built from `DoclingDocument` JSON, not from a parallel OOXML walk.
3. **No external renderer.** Docling-only, as established across the whole project.
4. **One contract, all formats.** The manifest shape must not assume PPTX. PDF/DOCX/XLSX adapters produce the same `DoclingDocument` → same manifest.

## DoclingDocument JSON — verified structure (schema 1.10.0)

Confirmed by inspecting a real run (AFTO deck, 27 slides):

| JSON key | Shape | Maps to |
| --- | --- | --- |
| `pages` | dict `{page_no_str: {size:{width,height}, page_no}}` | `units` (one per page/slide); `size` in EMU |
| `groups` | list; slide groups have `name:"slide-N"`, `label:"chapter"`, `children:[$ref]` | per-slide content grouping |
| `texts` | list; each `{self_ref, parent, label, prov:[{page_no, bbox, charspan}], text, orig}` | text `blocks`; `label` ∈ {title, paragraph, list_item, text} |
| `tables` | list; `{prov, data:{table_cells:[...], num_rows, num_cols, grid}}` | table `blocks` **with cells** |
| `pictures` | list; `{prov, image:{mimetype, dpi, size, uri}}` | picture `blocks` + assets |
| `body` | `{children:[$ref]}` | document reading order (tree root) |

Coordinate note: `prov[].bbox` is `{l, t, r, b, coord_origin:"BOTTOMLEFT"}` in EMU. The manifest uses TOPLEFT `{x, y, cx, cy}`. Conversion (per page height `H`): `x=l`, `y=H-t`, `cx=r-l`, `cy=t-b`. A single `normalize_bbox` helper owns this — never inline the flip.

## Architecture

```
docling_document.py   NEW  — reads DoclingDocument JSON → units/blocks/tables/pictures
                              + reading order + normalized bbox. Format-agnostic.
ooxml_enrichment.py   NEW  — PPTX-only. Four functions, each keyed to a slide:
                              speaker notes, typography, theme, background.
                              Built by demoting experiments 2–5's ooxml.py/theme.py/
                              typography.py/placeholder_resolver.py/text_styles.py/
                              background.py to enrichment status.
image_captioner.py    NEW  — vision-LLM captioning of extracted picture assets
                              via Bedrock; provider abstraction mirrors semantics.py.
manifest_builder.py   REBUILT — orchestrates: Docling spine → OOXML enrichment →
                              image captions → Bloom → manifest.
bloom_classifier.py / semantics.py — unchanged; run on the Docling-built blocks.
schema.py             — block.source records "docling" vs enrichment provenance.
```

The join: OOXML enrichment attaches typography to a Docling text block by matching
`(page_no, bbox)`. Docling derives its bbox from the same OOXML shape coords, so the
match is reliable — but must be built behind a `match_text_to_shape` helper and
validated, not assumed.

## Iteration plan (the `/loop` drives this)

1. ✅ Investigate `DoclingDocument` JSON structure. (done — this spec.)
2. `docling_document.py` — parse pages/groups/texts/tables/pictures into units+blocks; `normalize_bbox`; reading order. Test against all 7 fixtures' existing `document.json`.
3. `manifest_builder` rebuilt on the Docling spine; OOXML enrichment re-attached as `(page,bbox)`-joined typography/notes/theme/background. All 7 fixtures extract.
4. `image_captioner.py` — Bedrock vision captioning of picture assets, provider-abstracted, fail-open.
5. Real Bedrock Bloom on the corpus; measure fallback fraction drop.
6. Scale audit — grep for hardcoded paths/thresholds/IDs; parameterize everything; confirm a fresh unseen PPTX runs unmodified.

## Definition of Done

- All 7 fixtures produce a manifest from the Docling spine; pytest green.
- Table blocks carry real cells (from `tables[].data.table_cells`).
- Typography/notes/theme/background still present, now as `(page,bbox)`-joined enrichment.
- Picture assets carry vision-LLM captions; opaque-block count drops.
- Bloom runs via Bedrock; `fallbackBlockFraction` measurably down.
- Zero hardcoded paths/thresholds/model IDs — all parameterized.
- `docling-role.md` decision record: Docling is the spine for all formats; OOXML is PPTX enrichment only; this never silently drifts again.
