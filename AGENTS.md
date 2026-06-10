# Agent Handoff: Docling Deep Extraction + PowerPoint Courseware Prototype

This repo's active document-extraction prototype is `tests/prototype`.

Do not build against `tests/trash/experiments/*`; those folders are archived
research history only.

## Current Build Target

The active implementation target is the PRD at:

```text
.specify/specs/2026-05-20-extend-metadata/
```

The current production-facing deep-extraction package is:

```text
docling_serve/deep_document/
```

PowerPoint/courseware-only instructional analysis is isolated in:

```text
docling_serve/powerpoint_courseware/
```

The prototype harness is:

```text
tests/prototype/
```

## Required Verification Loop

For every change to deep extraction, PowerPoint courseware behavior, or preview output, run:

```bash
uv run python tests/prototype/run_experiment.py
uv run pytest tests/test_env_parsing.py tests/test_config_file_loading.py tests/prototype/tests/ tests/test_deep_document_options.py tests/test_deep_document_docling_adapter.py tests/test_deep_document_export.py
```

Then publish the generated preview bundle to:

```text
/opt/captify-apps/captify-core-wiki/public/
```

The preview page is:

```text
https://dev.captify.io/preview.html
```

The preview must continue showing:

- rendered slide content
- original-slide PNG reference pane rendered from the same-presentation PDF
- extracted slide render pane
- color-coded `Slide JSON Object` pane
- speaker notes
- Bloom taxonomy
- image context for content images when available
- `Course Model JSON`
- `Source Item JSON` for extracted items

## Audit File

Keep `tests/audit.md` current after each loop. It is the audit agent's entry
point and must include:

- what changed
- commands run
- test results
- preview URL
- published artifact list
- remaining gaps

## Current Artifact Contract

The prototype emits:

- `pptx-ooxml-geometry.json`
- `canvas-contract.json`
- `pptx-ooxml-geometry.tldr`
- `summary.json`
- `course-model.json`
- `course-analysis-summary.json`
- `reengineering-input.json`
- `enriched-manifest.json`
- `multi-format-summary.json`
- `multi-format/{pptx,docx,xlsx,pdf}/*`
- `schemas/*.schema.json`
- `preview.html`
- `assets/*`
- `slide-png/*`
- `pdf-reference-map.json`
- `extraction-comparison-summary.json`

The PowerPoint courseware stage must not mutate the source extraction artifact.
It may emit `enriched-manifest.json` as a derived convenience copy.

## Service Integration

Generic service integration is `extraction=deep` under
`docling_serve/deep_document/`. Deep mode is structural only: no course model,
Bloom taxonomy, module inference, or pedagogical fields are emitted by the
server's generic extraction path.

PowerPoint courseware analysis lives in `docling_serve/powerpoint_courseware/`
and is used by the prototype/preview harness when working with PPT training
courseware. Do not wire it into default PDF/DOCX/XLSX/image extraction.

## Current Status

Last known good result:

```text
uv run python tests/prototype/run_experiment.py
uv run pytest tests/test_env_parsing.py tests/test_config_file_loading.py tests/prototype/tests/ tests/test_deep_document_options.py tests/test_deep_document_docling_adapter.py tests/test_deep_document_export.py
66 passed
```

The published preview bundle was refreshed at 2026-05-21 02:30 UTC and smoke
checked with:

```bash
curl -k -sSI https://dev.captify.io/preview.html
curl -k -sSI https://dev.captify.io/schemas/course-model.schema.json
curl -k -sSI https://dev.captify.io/multi-format/xlsx/course-model.json
curl -k -sSI https://dev.captify.io/slide-png/slide-001.png
curl -k -sSI https://dev.captify.io/pdf-reference-map.json
curl -k -sSI https://dev.captify.io/extraction-comparison-summary.json
rg -n "styleDefinition|Medium Style|background:#8585E0|font-size:12.00pt|extracted-table" /opt/captify-apps/captify-core-wiki/public/preview.html
rg -n "spacingBefore|spacingAfter|line-height:normal|&nbsp;|text-indent" tests/prototype/out/pptx-ooxml-geometry.json tests/prototype/out/preview.html
rg -n "Embedded image extraction|KIT/PARTS REQUIRED TO MODIFY SPARES|OCR words" /opt/captify-apps/captify-core-wiki/public/preview.html /opt/captify-apps/captify-core-wiki/public/pptx-ooxml-geometry.json
google-chrome --headless --disable-gpu --no-sandbox --screenshot=/tmp/docling-preview-spacing.png --window-size=1600,14000 https://dev.captify.io/preview.html
google-chrome --headless --disable-gpu --no-sandbox --screenshot=/tmp/docling-preview-unoserver-ocr.png --window-size=1800,14000 https://dev.captify.io/preview.html
google-chrome --headless --disable-gpu --no-sandbox --screenshot=/tmp/docling-preview-text-image-slides.png --window-size=1800,14000 https://dev.captify.io/preview.html
```

The PDF reference file for the AFTO fixture is:

```text
tests/test_files/1220dd73-5621-458d-950e-657a6738fb14-updated AFTO Form 874 for presentation.pdf
```

The matcher maps the 27 extracted PPTX slides into the 41-page PDF and skips
interleaved non-slide/comment pages. `pdf-reference-map.json` records the
selected PDF page and match score for each slide.

Current extraction fidelity note: `extraction-comparison-summary.json` compares
all 27 extracted slides against the PDF reference. Current result is 27/27
matched slides, lowest PDF token coverage 0.75, and 0 flagged slides. Inherited
body-placeholder bullets, structured table rows/cells, and table text are now
included in extracted text, course-model normalization, comparison coverage,
and `preview.html` rendering.

Current style-capture note: PPTX extraction now captures normalized element
style metadata plus raw OOXML for shape/text/table structures. Tables preserve
`styleId`, `styleDefinition` from `ppt/tableStyles.xml`, columns, row heights,
cell paragraphs/runs, fills, borders, effective cell styles, and raw table/cell
XML. The preview uses captured table fills/borders/fonts for the current AFTO
table slides, including inherited white/bold header text. Paragraphs now carry
`spcBef`, `spcAft`, `lnSpc`, `marL`, `indent`, empty paragraph markers, and
raw paragraph attrs, and the preview renders those rather than fixed paragraph
spacing. This is capture-first, not yet a complete PowerPoint renderer:
production should keep raw OOXML in a sidecar/debug artifact and project a
smaller normalized style contract into API responses.

Current embedded-image note: slide-form screenshots embedded as content images
are now passed through local Tesseract OCR plus OpenCV grid detection. This is
generic for content images and skips master/header images. The latest AFTO run
recovered OCR/grid data for 11/11 content image assets with 856 OCR words and
zero LLM tokens. Slide 14's embedded Part C form now exposes text such as
`KIT/PARTS REQUIRED TO MODIFY SPARES`, word boxes, confidence, and detected
table/grid lines in both `pptx-ooxml-geometry.json` and `preview.html`.

Current preview layout note: slides 14-21 contain a prose text placeholder plus
a lower form image. The OOXML text placeholder bbox covers most of the slide,
so the preview renderer must not treat overlap with the lower image as a
side-by-side collision. `preview_text_bbox()` now clips body text render boxes
above content images while preserving the original extracted bbox in JSON. The
preview also adds `data-element-id` markers so auditors can inspect generated
HTML by extracted element id.

Current course-model note: `course-model.json` now uses the deck title
`TIME COMPLIANCE TECHNICAL ORDER SUPPLY DATA REQUIREMENTS` for course metadata,
module title, and inferred objective topic. The builder consumes
`provider_from_environment()` and stamps the selected provider ID into
inference records and `providerUsage`. Bedrock structured-output mode is
configuration-validated and provider-stamped, but real Bedrock inference calls
are still not implemented in the builder. Thresholds/confidence values are now
named constants. Schemas are stricter and validate nested course-model records
against object-specific contracts. Fixture coverage includes explicit
task/condition/standard objective parsing, positive assessment detection,
positive redundancy detection, and ZIP artifact inclusion before return/upload.

Raw OOXML contract note: production should keep normalized style fields in the
primary manifest/API and persist raw OOXML as a sidecar/debug artifact. The
decision is documented in:

```text
.specify/specs/2026-05-20-extend-metadata/raw-ooxml-contract.md
```

`unoserver` experiment result: local execution is blocked in this environment
because there is no `libreoffice`, no `soffice`, no Python `uno` module, and no
`unoserver` package. Per the upstream project, `unoserver` is a LibreOffice UNO
server/client wrapper, so installing the Python package alone would not provide
a renderer. In the ATO environment where LibreOffice/soffice cannot be
installed, this is not a viable production conversion dependency.

Deterministic course-model classification was tightened after audit feedback:
objective inference no longer defaults to AFTO-specific text, watermark lines
are stripped before classification, substring false positives are avoided,
slide records include `primaryRole`, and repeated deck headers are separated
from semantic slide titles where the extractor can infer a better title.
