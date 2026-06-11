# Prototype Findings

## Purpose

The prototype tested the no-renderer PPTX path:

- open the `.pptx` as a ZIP of OOXML parts
- parse slide XML directly
- preserve shape coordinates from PowerPoint's native EMU coordinate system
- normalize those coordinates into inches and canvas pixels
- extract text runs, typography, media references, tables, backgrounds, and notes
- resolve slide-local placeholder geometry from layout/master placeholders
- generate a JSON artifact that can feed a tldraw/canvas renderer

No PDF conversion, LibreOffice, soffice, COM, Spire, Aspose commercial, or
watermarked renderer was used.

## Run Result

```json
{
  "status": "complete",
  "slideCount": 27,
  "contentElementCount": 67,
  "masterDecorationElementCount": 81,
  "elementCount": 148,
  "textElementCount": 52,
  "imageElementCount": 11,
  "tableElementCount": 2,
  "assetCount": 13,
  "xmlPartCount": 114,
  "reviewRequiredElementCount": 1,
  "rendererUsed": false
}
```

## Generated Artifacts

- `tests/prototype/out/pptx-ooxml-geometry.json`
- `tests/prototype/out/canvas-contract.json`
- `tests/prototype/out/pptx-ooxml-geometry.tldr`
- `tests/prototype/out/preview.html`
- `tests/prototype/out/summary.json`
- `tests/prototype/out/assets/*`
- `tests/prototype/out/xml/*`

The primary artifact is `pptx-ooxml-geometry.json`.

## JSON Contract

Each slide has:

- slide ID, index, slide number, title, layout name
- source OOXML part
- source relationships
- native EMU size
- normalized inch size
- normalized canvas pixel size
- background
- decorations
- speaker notes
- positioned elements

Each element has:

- stable element ID
- source slide part, shape index, shape name, relationship ID, placeholder type,
  and placeholder index
- type/kind
- z-index
- bbox in EMU, inches, and pixels
- bbox source (`slide_shape`, `layout_placeholder`, `master_placeholder`, or
  unresolved)
- rotation
- editable flag and target canvas layer
- plain text
- resolved paragraphs/runs
- asset ID / relationship ID when applicable
- visual style for inherited master shapes
- quality warnings

Example coordinate shape:

```json
{
  "bbox": {
    "emu": { "x": 0, "y": 1094874, "w": 9144000, "h": 3128211 },
    "inches": { "x": 0.0, "y": 1.1974, "w": 10.0, "h": 3.4211 },
    "px": { "x": 0.0, "y": 114.95, "w": 960.0, "h": 328.42 }
  }
}
```

## What This Proves

This is better aligned to the desired product than screenshot rendering:

- all 27 slides were captured
- all relevant `ppt/**/*.xml` and `ppt/**/*.rels` parts were indexed and copied
- inherited placeholder geometry works: 19 slide-local zero-size placeholders
  were resolved from master placeholder geometry
- visible inherited slide-master artwork is now extracted as locked canvas
  elements: left logo, right crest, and the blue header rule on each slide
- no watermark risk exists
- no license-cost renderer is required
- text remains editable
- image assets remain separate reusable assets
- slide notes remain available
- tldraw can receive shape-level geometry and real extracted image assets
  instead of one flat rendered page per slide

The file-type-neutral canvas contract is now the best interface target for the
future web viewer:

- `units[]` are slides now, but can also represent PDF pages, DOCX sections, or
  XLSX sheets later
- `shapes[]` carry pixel bboxes, source links, editable text, asset IDs, and
  quality status
- layers separate slide frames, backgrounds, assets, editable content,
  master decorations, structural placeholders, and quality overlays

## Published Preview Review

Published files in `captify-core-wiki/public`:

- `/preview.html`
- `/pptx-ooxml-geometry.json`
- `/canvas-contract.json`
- `/pptx-ooxml-geometry.tldr`
- `/summary.json`
- `/assets/*`

Live route verified:

- `https://dev.captify.io/preview.html` returned `HTTP/2 200`
- `https://dev.captify.io/pptx-ooxml-geometry.json` returned `HTTP/2 200`
- `https://dev.captify.io/canvas-contract.json` returned `HTTP/2 200`
- `https://dev.captify.io/pptx-ooxml-geometry.tldr` returned `HTTP/2 200`
- `https://dev.captify.io/summary.json` returned `HTTP/2 200`
- extracted image assets under `/assets/` returned `HTTP/2 200`

Visual review pass:

- master logos and header rule now render across the deck
- invisible no-fill/no-line master text boxes are filtered out
- title text uses generic collision-aware insets when it intersects inherited
  master images, so long titles wrap inside the safe header area instead of
  overlapping logos
- each slide is now shown in a review row with the slide scaled down and a
  right-side panel for speaker notes and Bloom taxonomy metadata
- Bloom metadata is included per slide as deterministic heuristic output and is
  explicitly marked for LLM/Bedrock review before production training use
- page-content images are captioned with Bedrock vision and shown in the
  side-panel Image Context section; inherited header/master images are excluded
  from both preview rendering and image-caption processing
- ordered OOXML text breaks are preserved, so title/subtitle-style runs inside a
  single PowerPoint title shape render on separate lines with their source font
  family, size, bold, underline, and color
- each slide now exposes a `slideFormat` object with layout, layout part, master
  part, theme file/name, background, size, source parts, and title line/runs
- the preview is acceptable as a commercial inspection proof for this training
  deck, with the caveat that the final product renderer should be tldraw-native
  rather than this static HTML proof

This should become the canonical PPTX deep-mode artifact. Rendered PDF/PNG
references can still be added later as an optional validation/reference layer,
but they should not be the source of truth.

## Important Gaps

This is structural extraction, not pixel-perfect rendering. Remaining work:

- group shape child coordinate transforms need production hardening
- connectors and complex preset geometries need richer shape mapping
- charts and SmartArt need either structured extraction or explicit opaque
  placeholders
- one table element still reports `zero_or_negative_bbox`; table geometry needs
  the same kind of explicit fallback/normalization pass
- tables need full cell geometry/styling extraction
- image-only form examples are preserved as assets, but are not yet decomposed
  into editable form fields
- animations/transitions are not represented
- theme/master/layout inheritance should stay covered by regression tests
- the `.tldr` file is a proof artifact, not a final production importer

## Recommendation

Move PPTX deep mode to an OOXML-first architecture:

1. Generate `pptx-ooxml-geometry.json`.
2. Generate `canvas-contract.json`.
3. Persist extracted media assets and indexed XML parts.
4. Render editable shapes in tldraw from the canvas contract.
5. Use Bedrock only for semantic enrichment, Bloom taxonomy alignment, image
   understanding, chart summarization, and low-confidence structure decisions.
6. Add optional rendered page references only when an ATO-approved renderer is
   available.

## Verification

Commands run:

```bash
uv run python tests/prototype/run_experiment.py
uv run pytest tests/prototype/tests/
```

Result:

- `9 passed`
- `rendererUsed: false`
- `xmlPartCount: 114`
- `reviewRequiredElementCount: 1`
- generated JSON artifacts parsed successfully
- pytest cache folders were removed
