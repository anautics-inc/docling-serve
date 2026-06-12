# Experiment 2: Docling-Only Deep Document JSON Contract

**Date:** 2026-05-19
**Parent PRD:** `prd.md` (in this folder)
**Supersedes experiment1 for production direction.** Experiment1 stays as a historical artifact.

---

## Goal

Produce **one portable JSON file per PPTX** that captures everything a downstream canvas (tldraw, custom React, print preview, agent inspector) needs to reconstruct and reason about the deck — using **only Docling and OOXML** as extractors. The JSON must be tagged top-to-bottom with Bloom's revised taxonomy so a training builder can ask "which slides are recall-only?" and get a defensible answer.

The output is **viewer-agnostic**. No tldraw shape JSON. No widget coordinates. Consumers (tldraw runtime, captify canvas service, agents) translate the contract into their own rendering primitives.

---

## Hard Constraints (do not negotiate)

1. **Docling is the only renderer.** No LibreOffice, no soffice, no Aspose, no headless Chrome, no python-pptx rasterization. If a full-slide image isn't available from Docling, the contract describes that gap explicitly — it does not fake a render.
2. **OOXML is allowed and expected** as a structural supplement to Docling (slide order, notes, embedded media binaries, placeholder roles, shape positions in EMU). OOXML is parsing, not rendering.
3. **One portable JSON file** is the deliverable, plus referenced asset binaries on disk. The file must round-trip through `json.loads(json.dumps(x))` and validate against a published JSON Schema.
4. **Bloom alignment is mandatory at every leaf.** Slide-level, block-level (text frames, tables, pictures, notes). No untagged content.
5. **Run against all 7 fixtures** in `tests/test_files/`, not just the AFTO deck. Emit a cross-fixture comparison table.
6. **Local output paths must mirror future S3 keys** (PRD §161). Flat `out/` is forbidden.
7. **No invented tldraw shapes.** Drop `tldrawCommands` from the artifact. Replace with `canvasUnits` describing logical content, positions in slide-native EMU/percent, and Bloom tags.

---

## What experiment1 got right (keep)

- Docling CLI invocation: `--to md --to json --to html --image-export-mode referenced`.
- OOXML traversal for slide order, notes, and media relationships (`run_experiment.py:128-194`).
- Stable asset IDs (`pptx-media-slide-{number}-{relId}`).
- `deterministic_fallback` provider label on Bloom classifier when no verb match.
- Bedrock structured-output schema sketched for production swap-in.

## What experiment1 got wrong (do not repeat)

- python-pptx + Pillow "rough render" wired into the canvas as `slideImageRef`. **Delete this entire path.**
- Raw tldraw `geo` shapes emitted in `tldrawCommands`. **Delete.**
- Single hardcoded fixture (AFTO). **Replace with multi-fixture loop.**
- Flat `out/` directory. **Replace with S3-shaped paths.**
- Manifest missing `extractionId`, `effectiveOptions`, `outputs`, `errors`, top-level `assets[]`. **Align with PRD §94.**
- Per-slide `title = text.splitlines()[0]`. **Use PPTX title placeholders.**
- Docling tables and pictures only counted, never mapped per-slide. **Map them.**
- Speaker notes captured verbatim including junk like `"3"`. **Filter.**

---

## Output Contract

### File layout (mirrors future S3)

```
tests/experiment2/out/
├── _tenant=local/
│   └── spaces/
│       └── dataset=experiment2/
│           └── {documentId}-{originalFileName}/
│               ├── source.pptx                              # original (copy)
│               ├── extractions/
│               │   └── {extractionId}/
│               │       ├── manifest.json                    # the portable JSON contract
│               │       ├── docling/
│               │       │   ├── document.json
│               │       │   ├── document.md
│               │       │   ├── document.html
│               │       │   └── images/                      # Docling referenced PNGs
│               │       ├── ooxml/
│               │       │   └── media/                       # OOXML-extracted embedded media
│               │       └── effective_options.json
└── _summary/
    ├── comparison.csv                                       # one row per fixture
    └── comparison.json
```

- `documentId` = `sha256(originalFileName)[:16]` for now. Replaceable with a real document ID once Captify supplies one.
- `extractionId` = `sha256(json.dumps({sourceSha256, mode, doclingVersion, doclingServeVersion, bedrockModels, effectiveOptions}, sort_keys=True))[:16]`. Re-running with identical inputs must yield the same extractionId.

### `manifest.json` — top-level shape

```jsonc
{
  "schemaVersion": "2.0",
  "artifactKind": "deep_document_manifest",
  "documentId": "string",
  "documentType": "pptx",
  "createdAt": "ISO-8601 UTC",

  "source": {
    "originalFileName": "string",
    "sizeBytes": 0,
    "sha256": "string",
    "contentType": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "localPath": "string"             // path to copied source.pptx
    // bucket/key/versionId are reserved and emitted as null in local mode
  },

  "extraction": {
    "mode": "deep",
    "extractionId": "string",
    "status": "complete" | "partial" | "failed",
    "startedAt": "ISO-8601",
    "completedAt": "ISO-8601",
    "doclingServeVersion": "string",
    "doclingVersion": "string",
    "effectiveOptions": { /* exact options sent to docling */ },
    "bedrockModels": [ { "purpose": "taxonomy", "modelId": "anthropic.claude-4.5-sonnet" } ]
  },

  "outputs": {
    "manifestKey": "string",          // self-reference
    "doclingJsonKey": "string",
    "markdownKey": "string",
    "htmlKey": "string",
    "effectiveOptionsKey": "string"
  },

  "units": [ /* see Unit shape below */ ],
  "assets": [ /* see Asset shape below — top-level table for cross-slide reuse */ ],
  "taxonomy": { /* see Taxonomy shape below */ },

  "diagnostics": {
    "slideCount": 0,
    "doclingPageCount": 0,
    "doclingTextItemCount": 0,
    "doclingTableCount": 0,
    "doclingPictureCount": 0,
    "ooxmlMediaCount": 0,
    "ooxmlNotesSlideCount": 0,
    "renderable": "structural_only",    // "structural_only" | "image_available"
    "renderAvailability": "Docling does not produce full-slide rasters for PPTX; canvas must render from structure."
  },

  "errors": [
    { "stage": "string", "unitId": "string|null", "message": "string", "recoverable": true }
  ]
}
```

### `Unit` shape (one per slide)

```jsonc
{
  "unitId": "slide-001",                // stable, zero-padded by slide number
  "unitType": "slide",
  "index": 0,                            // zero-based slide order
  "slideNumber": 1,                      // one-based PPTX number
  "sourcePart": "ppt/slides/slide1.xml",

  "title": "string|null",                // pulled from <p:ph type='title'>, NOT splitlines()[0]
  "layoutName": "string|null",           // slideLayout reference name when discoverable
  "slideSizeEmu": { "cx": 9144000, "cy": 6858000 },

  "blocks": [
    // Every renderable shape on the slide becomes a block.
    // bbox is in EMU on the slide-native coordinate system.
    {
      "blockId": "slide-001-block-001",
      "kind": "text" | "table" | "picture" | "chart_placeholder" | "smartart_placeholder" | "group" | "other",
      "ooxmlShapeIndex": 1,
      "placeholderType": "title|body|ctrTitle|subTitle|null",
      "bbox": { "x": 0, "y": 0, "cx": 0, "cy": 0 },
      "zOrder": 0,
      "text": "string|null",             // for text blocks
      "tableRef": "table-id|null",       // FK into Docling tables for table blocks
      "assetId": "asset-id|null",        // FK into manifest.assets for picture blocks
      "classification": { /* Bloom — see below */ }
    }
  ],

  "speakerNotes": {
    "raw": "string",
    "cleaned": "string|null",            // null if cleaned == ""; junk filter applied
    "junkFiltered": true | false,
    "classification": { /* Bloom for the notes specifically */ }
  },

  "doclingProvenance": {
    "pageNo": 1,
    "textItemIds": ["..."],              // Docling JSON $ref-style ids when extractable
    "tableItemIds": [],
    "pictureItemIds": []
  },

  "classification": { /* aggregated slide-level Bloom */ }
}
```

### `Asset` shape (top-level table)

```jsonc
{
  "assetId": "asset-{sha8}",             // hash of binary, NOT slide+relId
  "kind": "image",
  "mimeType": "image/png",
  "sizeBytes": 0,
  "sha256": "string",
  "localPath": "string",
  "sourceParts": ["ppt/media/image1.png"],
  "usedBy": [
    { "unitId": "slide-001", "blockId": "slide-001-block-002", "relationshipId": "rId4" },
    { "unitId": "slide-014", "blockId": "slide-014-block-001", "relationshipId": "rId2" }
  ],
  "doclingPictureId": "string|null",     // if Docling's referenced-image set includes this binary
  "classification": { /* Bloom on the image — fallback rule when no VLM */ }
}
```

Hashing assets by content sha (not slide+relId) gives correct cross-slide reuse and stable IDs across re-runs.

### `Taxonomy` shape

```jsonc
{
  "taxonomy": "bloom_revised",
  "taxonomyVersion": "1.0",
  "classificationProvider": "deterministic_fallback" | "aws_bedrock_structured_output",
  "levels": [
    { "id": "remember", "label": "Remember", "definition": "..." }
    // … 6 levels
  ],
  "instructionalRoles": [
    "title", "learning_objective", "concept_explanation", "procedure",
    "example", "knowledge_check", "summary_or_closing", "reference", "appendix"
  ],
  "deckSummary": {
    "slideCount": 0,
    "blockCount": 0,
    "bloomDistribution": { "remember": 0, "understand": 0, /* … */ },
    "roleDistribution": { /* role: count */ },
    "dominantBloomLevel": "string",
    "higherOrderBlockCount": 0,
    "higherOrderSlideCount": 0,
    "lowestConfidence": 0.0,
    "averageConfidence": 0.0,
    "providerCoverage": { "deterministic_fallback": 0.0, "aws_bedrock_structured_output": 0.0 }
  }
}
```

### Bloom `classification` shape (used at slide, block, asset, and notes level)

```jsonc
{
  "level": "remember|understand|apply|analyze|evaluate|create",
  "role": "title|learning_objective|concept_explanation|procedure|example|knowledge_check|summary_or_closing|reference|appendix",
  "confidence": 0.0,                     // 0..1
  "provider": "deterministic_fallback" | "aws_bedrock_structured_output",
  "method": "string",                    // e.g. "verb_match", "default_no_direct_bloom_verb", "bedrock_claude_4_5"
  "evidence": [
    { "source": "block_text" | "speaker_notes" | "table_headers" | "image_caption" | "aggregate",
      "excerpt": "string",
      "terms": ["string"],
      "reason": "string|null" }
  ],
  "recommendedImprovements": [
    { "targetBloomLevel": "string", "suggestion": "string" }
  ]
}
```

**Aggregation rule for slide-level classification** (when no Bedrock call is made): take the highest Bloom level present across the slide's blocks + notes, weighted by content length. Document the rule in the implementation comments; do not bake magic constants.

**Aggregation rule for the deck**: count distinct blocks (not slides) into `bloomDistribution`. `higherOrderBlockCount` = sum of `analyze + evaluate + create`. This is what answers "is the deck recall-only?"

---

## Pipeline

```
for fixture in tests/test_files/*.pptx:
    1. compute source sha256, documentId, extractionId
    2. mkdir tests/experiment2/out/_tenant=local/.../extractions/{extractionId}/
    3. copy source.pptx (link, not full copy, if feasible)
    4. run Docling: --to md --to json --to html --image-export-mode referenced
       → docling/document.{json,md,html} + docling/images/*.png
    5. parse OOXML:
       - slide order (slide_numbers)
       - per slide: title placeholder, body placeholders, shape bboxes in EMU, zOrder
       - speaker notes XML
       - relationship-based media binaries → ooxml/media/*
    6. merge Docling JSON tables/pictures into per-slide blocks via prov[].page_no
    7. dedupe assets by sha256, build top-level assets[] with usedBy[]
    8. junk-filter speaker notes (numeric-only, single-char, whitespace, == slide number)
    9. classify Bloom:
       - per text block (verb match → level, fallback understand@0.28)
       - per table block (default analyze@0.4 when ≥3 cols and headers present, else understand@0.3 — call this out as a heuristic)
       - per picture asset (default understand@0.2 with explicit "no_caption_available" reason until VLM lands)
       - per notes (verb match on cleaned text)
       - per slide (aggregate: highest non-fallback level present, weighted by content)
   10. build deck summary
   11. write manifest.json + effective_options.json
   12. validate manifest against jsonschema (fail loud)

after loop:
    13. emit _summary/comparison.csv and comparison.json
```

---

## Acceptance Criteria

The experiment is **done** when all of the following are true for every fixture in `tests/test_files/`:

1. `manifest.json` exists at the S3-shaped path.
2. `manifest.json` validates against the published JSON Schema (ship the schema in `tests/experiment2/schema/deep-document-manifest.schema.json`).
3. `manifest.extraction.extractionId` is byte-identical on a re-run with no inputs changed.
4. `manifest.units` length == OOXML slide count == Docling page count, OR the discrepancy is recorded in `errors[]` with a recoverable=true entry.
5. Every `unit.blocks[i].classification.level` is set; no `null` Bloom levels.
6. Every `asset.classification.level` is set.
7. Every `unit.speakerNotes.classification.level` is set when `cleaned` is non-empty; `null` otherwise.
8. No `slideImageRef` field exists anywhere in the output. The contract is structural-only.
9. No `tldrawCommands`, `tldraw shapes`, or canvas-runtime-specific fields exist in the manifest. `canvasUnits` (or equivalent viewer-agnostic blocks array) is the only thing a viewer needs.
10. `_summary/comparison.csv` has one row per fixture with columns:
    `fixture, slides, blocks, text_blocks, table_blocks, picture_blocks, ooxml_media, docling_pictures, notes_slides_raw, notes_slides_cleaned, dominant_bloom, higher_order_slides, dominant_role, avg_confidence, docling_seconds, total_seconds, errors`.
11. Re-running the experiment is idempotent: same inputs → byte-identical `manifest.json` (`createdAt` field excluded from the diff).
12. Junk-notes filter is observable: at least one fixture must show `notes_slides_raw > notes_slides_cleaned` in the comparison CSV (the AFTO deck's slide-3 "3" qualifies).

---

## Bloom Tagging Rules — Be Explicit

The user's requirement is *"everything tagged and aligned with Bloom."* That means:

- **Slide-level tag**: aggregate, NOT a re-classification. It must be derivable from block tags and provide an audit trail (`evidence.source = "aggregate"`, list the block IDs that contributed).
- **Text block tag**: verb match against `BLOOM_VERBS` (already defined in `scripts/prototype_deep_document_artifact.py`). On no match → `understand@0.28` with `method: "default_no_direct_bloom_verb"`. This is fine *as a fallback* — but the deck summary must report what fraction of blocks rely on the fallback. Builders will trust the data only if that number is visible.
- **Table block tag**: heuristic based on shape (rows × cols, header presence). Document the rule. Tables comparing options → `analyze`; tables of reference values → `remember`.
- **Picture asset tag**: until VLM caption is available, default to `understand@0.2` with explicit `method: "no_caption_available"`. Builders should see "we don't know yet" — not "this image teaches understanding."
- **Notes tag**: same rule as text blocks, applied to the cleaned notes only.

**Critical:** the per-block classification is a *training-author signal*, not a guarantee. Confidence < 0.5 must be honored downstream as "needs review." The Bedrock production swap will raise confidence; the deterministic baseline must never exceed 0.84 (already enforced in experiment1's classifier — keep that ceiling).

---

## What to Discover (open questions for the agent to answer in `experiment2-findings.md`)

1. Does Docling 2.x's `image_export_mode=referenced` for PPTX export every embedded picture, or does it skip pictures referenced only from layouts/masters? Compare Docling's `images/` count to OOXML media count across all 7 fixtures.
2. Does Docling's `DoclingDocument.tables[].prov[].page_no` reliably map to PPTX slide numbers? If not, what's the right join key (text neighborhood matching? bbox heuristic?)
3. What's the slowest fixture and where does the time go (Docling vs OOXML vs classification)? Record per-stage seconds.
4. For chart-heavy decks (the 5.7 MB "Interchangeability and Substitutability" file is likely the worst), does Docling capture anything chart-shaped, or do charts arrive as opaque `chart_placeholder` blocks with no text? Document the gap.
5. How often does Docling and OOXML disagree on picture count? Slide-3 in AFTO showed delta = 2. Across all 7 decks, what's the distribution?
6. How much of the deck classifies via fallback vs verb-match? Report `providerCoverage` per fixture.

---

## Out of Scope (do not build)

- Bedrock VLM picture description. Defer; the contract has the shape for it but uses the `no_caption_available` fallback.
- Real Bedrock taxonomy calls. Keep `deterministic_fallback`; the schema and provider hook must exist so a Bedrock call is a drop-in.
- Real S3 upload. Local paths mirror S3 shape but no boto3 calls.
- Full Docling-side rendering investigation beyond `--image-export-mode referenced`. If Docling's PPTX path can't produce full-slide rasters, the manifest's `renderable: "structural_only"` says so and that's the answer.
- Per-tenant/per-dataset configurability. Use literal `_tenant=local` / `dataset=experiment2` for now.
- Validating the canvas projection visually. The deliverable is JSON. A separate experiment can build a viewer.

---

## File Map for the Agent

What to create:

```
tests/experiment2/
├── README.md                       # how to run; the agent updates this
├── run_experiment.py               # batch driver, one entry point
├── deep_document/                  # NEW package — refactor the inline logic out of run_experiment
│   ├── __init__.py
│   ├── docling_runner.py           # Docling CLI wrapper, captures effectiveOptions + versions
│   ├── ooxml_parser.py             # slide order, titles, placeholders, bboxes, notes, media rels
│   ├── manifest_builder.py         # merges Docling JSON + OOXML into manifest schema 2.0
│   ├── asset_indexer.py            # sha-based dedup, usedBy index
│   ├── bloom_classifier.py         # deterministic baseline + provider interface
│   ├── notes_cleaner.py            # junk filter
│   ├── ids.py                      # documentId, extractionId, blockId, assetId helpers
│   └── schema.py                   # jsonschema dict (also dumped to schema/ for inspection)
├── schema/
│   └── deep-document-manifest.schema.json
├── tests/                          # pytest tests for THIS experiment's logic
│   ├── test_ids_are_stable.py
│   ├── test_notes_cleaner.py
│   ├── test_bloom_aggregation.py
│   ├── test_manifest_schema_validates.py
│   └── test_fixture_smoke.py       # runs against test_files and asserts acceptance criteria
└── out/                            # gitignored; populated by run
```

What to delete or leave alone:

- **Leave `tests/experiment1/` untouched** as the historical baseline.
- **Do not call `scripts/prototype_render_canvas_preview.py`** from experiment2. Preview rendering is an experiment3 concern (a viewer experiment), not part of the contract.
- **Do not import from `scripts/prototype_deep_document_artifact.py`**. Copy the Bloom verbs/definitions/role patterns into `deep_document/bloom_classifier.py` so experiment2 is self-contained and the prototype script can be removed later without breaking anything.

---

## Sanity Checks Before Declaring Done

Run these commands and paste the output into `experiment2-findings.md`:

```bash
# 1. All fixtures processed
ls tests/experiment2/out/_tenant=local/spaces/dataset=experiment2/ | wc -l   # should equal 7

# 2. Idempotence
uv run python tests/experiment2/run_experiment.py
sha=$(find tests/experiment2/out -name manifest.json -exec sha256sum {} \; | sort)
# Re-run, strip createdAt before diff
uv run python tests/experiment2/run_experiment.py
sha2=$(find tests/experiment2/out -name manifest.json -exec sha256sum {} \; | sort)
diff <(echo "$sha") <(echo "$sha2")    # may differ only because of createdAt; the rest must be stable

# 3. No banned fields
grep -r '"slideImageRef"' tests/experiment2/out && echo "FAIL: slideImageRef leaked"
grep -r '"tldrawCommands"' tests/experiment2/out && echo "FAIL: tldrawCommands leaked"

# 4. Bloom coverage
uv run python -c "
import json, pathlib
for m in pathlib.Path('tests/experiment2/out').rglob('manifest.json'):
    doc = json.loads(m.read_text())
    untagged = [(u['unitId'], b['blockId']) for u in doc['units'] for b in u['blocks'] if not b.get('classification', {}).get('level')]
    print(m.name, 'untagged blocks:', len(untagged))
"

# 5. Schema validates
uv run python -m jsonschema -i path/to/manifest.json tests/experiment2/schema/deep-document-manifest.schema.json
```

---

## Reviewer Hand-Off

When the agent reports experiment2 done, the auditor (next pass) will check:

1. Acceptance criteria 1–12 above, line by line.
2. The `_summary/comparison.csv` for outliers: any deck where blocks < slides, or where `dominant_bloom = understand` AND `higher_order_slides = 0` (likely classifier under-firing), or where Docling/OOXML media delta exceeds 5.
3. That no shape-rendering code (Pillow draw calls on slide content) sneaks back in.
4. That the JSON Schema in `tests/experiment2/schema/` matches what the manifest actually emits.
5. That `experiment2-findings.md` answers the six "what to discover" questions with data, not prose.

If those pass, the contract is ready to wire into `app/api/<app>/entities/...` on the Captify side.
