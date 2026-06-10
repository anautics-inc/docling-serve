# Build Audit Log: Prototype Course Model

Updated: 2026-05-21 15:30 UTC

## Builder Note — 2026-05-21 (module boundary cleanup)

Courseware analysis has been moved out of the generic document-extraction path.
The format-neutral deep extraction implementation now lives under
`docling_serve/deep_document/`; PowerPoint/training-courseware analysis lives
under `docling_serve/powerpoint_courseware/` and is used by the prototype
preview harness only.

### Developer Review Packet

Review intent:

- Confirm generic `extraction=deep` supports PPT/PPTX, DOCX/Word, PDF, image,
  and XLSX/Excel as structural extraction without pulling in courseware logic.
- Confirm PowerPoint/training-courseware analysis is isolated in
  `docling_serve/powerpoint_courseware/`.
- Confirm no `docling_serve/course_model/` package or import remains.
- Confirm the prototype preview still renders and the generated courseware JSON
  remains schema-valid.

Primary files to review:

```text
docling_serve/app.py
docling_serve/settings.py
docling_serve/orchestrator_factory.py
docling_serve/rq_job_wrapper.py
docling_serve/deep_document/
docling_serve/powerpoint_courseware/
tests/test_deep_document_options.py
tests/test_deep_document_docling_adapter.py
tests/test_deep_document_export.py
tests/prototype/run_experiment.py
tests/prototype/run_slide_text_digest_review.py
tests/prototype/tests/test_course_model.py
tests/prototype/tests/test_multiformat_course_model.py
docs/deep-document-object-contract.md
docs/captify-s3-docling-upload-flow.md
AGENTS.md
```

Deep extraction behavior to verify:

- `POST /v1/convert/file` and `POST /v1/convert/file/async` accept
  `extraction`, `deep_s3_bucket`, and `deep_s3_prefix` form fields.
- `extraction=deep` fails fast with HTTP 503 if no request bucket and no server
  fallback bucket exist.
- `extraction=deep` forces `to_formats=[md,json,html,text]` plus referenced
  images while preserving any extra caller-requested export formats.
- Deep mode publishes an expanded object tree and returns a remote-target result
  instead of a ZIP/body document.
- `deep-document-package.json` is the root entrypoint; clients should resolve
  `entrypoints.deepDocuments[0]` rather than hardcoding a primary object path.

Format coverage to verify:

```text
PPT/PPTX -> document.units[].unitType = "slide"
PDF      -> document.units[].unitType = "page"
DOCX     -> document.units[].unitType = "section"
XLSX     -> document.units[].unitType = "sheet"
Image    -> document.units[].unitType = "image"
```

Courseware boundary to verify:

- `docling_serve/powerpoint_courseware/` may emit `course-model.json`,
  `course-analysis-summary.json`, `reengineering-input.json`, schemas, and
  `enriched-manifest.json`.
- `docling_serve/deep_document/` must remain structural only: no Bloom,
  module, objective, slide-role, or pedagogical fields.
- Legacy `DOCLING_SERVE_COURSE_MODEL_*` provider env names are still accepted
  by the PowerPoint courseware provider for compatibility, but new code should
  prefer `DOCLING_SERVE_COURSEWARE_*`.

Exact reproduction commands:

```bash
uv run python tests/prototype/run_experiment.py
uv run pytest tests/test_env_parsing.py tests/test_config_file_loading.py tests/prototype/tests/ tests/test_deep_document_options.py tests/test_deep_document_docling_adapter.py tests/test_deep_document_export.py
uv run ruff check docling_serve/powerpoint_courseware docling_serve/deep_document tests/test_deep_document_options.py tests/test_deep_document_docling_adapter.py tests/test_deep_document_export.py
python3 -m py_compile docling_serve/powerpoint_courseware/__init__.py docling_serve/powerpoint_courseware/builder.py docling_serve/powerpoint_courseware/artifact_writer.py docling_serve/powerpoint_courseware/pedagogy_provider.py docling_serve/powerpoint_courseware/schema_validation.py docling_serve/deep_document/options.py docling_serve/deep_document/docling_adapter.py docling_serve/deep_document/document_builder.py docling_serve/deep_document/export_results.py
rg -n "docling_serve\\.course_model|docling_serve/course_model|resources.files\\(\"docling_serve.course_model" docling_serve tests/prototype AGENTS.md docs
```

Expected results:

```text
run_experiment.py: completes with status complete
pytest: 66 passed, 27 warnings
ruff: All checks passed
py_compile: exit 0
course_model import grep: no matches
```

Preview publish/check commands:

```bash
cp -r tests/prototype/out/. /opt/captify-apps/captify-core-wiki/public/
curl -k -sSI https://dev.captify.io/preview.html
curl -k -sSI https://dev.captify.io/schemas/course-model.schema.json
curl -k -sSI https://dev.captify.io/pptx-ooxml-geometry.json
curl -k -sSI https://dev.captify.io/extraction-comparison-summary.json
python3 - <<'PY'
import json
from pathlib import Path
base = Path("/opt/captify-apps/captify-core-wiki/public")
summary = json.loads((base / "summary.json").read_text())
comparison = json.loads((base / "extraction-comparison-summary.json").read_text())
course = json.loads((base / "course-model.json").read_text())
print("status", summary["status"])
print("slides", summary["slideCount"])
print("matched", comparison.get("matchedSlideCount") or comparison.get("summary", {}).get("matchedSlideCount"))
print("issues", comparison.get("slidesWithIssues") or comparison.get("summary", {}).get("slidesWithIssues"))
print("course_modules", len(course["course"]["modules"]))
print("provider", course["providerUsage"]["provider"])
PY
```

Expected preview checks:

```text
Public URLs may return HTTP/2 307 to the wiki auth callback.
Local public file check should print:
status complete
slides 27
matched 27
issues 0
course_modules 19
provider deterministic
```

Known gaps / review risks:

- The old handoff referenced `tests/test_course_model_response_enrichment.py`
  and `tests/test_course_model_export_results.py`; those files are not present
  in this checkout. Equivalent available coverage is the prototype courseware
  suite plus deep-document export tests.
- Live S3 upload was unit-tested with a fake uploader path, not re-run against a
  real bucket in this loop.
- The restored PowerPoint courseware module is intentionally compact and
  prototype-oriented. It should not be generalized into default PDF/DOCX/XLSX
  extraction without a separate design pass.

Commands run:

```bash
uv run python tests/prototype/run_experiment.py
uv run pytest tests/test_env_parsing.py tests/test_config_file_loading.py tests/prototype/tests/ tests/test_deep_document_options.py tests/test_deep_document_docling_adapter.py tests/test_deep_document_export.py
```

Results:

```text
Prototype generation: status complete; 27 slides; 27/27 PDF reference matches;
lowest PDF token coverage 0.75; 0 flagged extraction-comparison slides;
11/11 content images with OCR/grid extraction; 856 OCR words.

Pytest: 66 passed, 27 warnings in 7.08s.
```

Preview URL:

```text
https://dev.captify.io/preview.html
```

Published artifact list is unchanged from the current artifact contract. The
remaining gap is that `tests/test_course_model_response_enrichment.py` and
`tests/test_course_model_export_results.py` named by older handoff text are not
present in this checkout; equivalent available coverage is now the prototype
courseware suite plus deep-document export tests.

Published to:

```text
/opt/captify-apps/captify-core-wiki/public/
```

Smoke checks:

```bash
curl -k -sSI https://dev.captify.io/preview.html
curl -k -sSI https://dev.captify.io/schemas/course-model.schema.json
curl -k -sSI https://dev.captify.io/pptx-ooxml-geometry.json
curl -k -sSI https://dev.captify.io/extraction-comparison-summary.json
```

Result: all four public URLs returned `HTTP/2 307` to the wiki auth callback.
Local published-file checks confirmed `preview.html`, schema files,
`pptx-ooxml-geometry.json`, and `extraction-comparison-summary.json` are present
under the public directory, with summary status `complete`, 27 slides, 27 PDF
reference matches, 0 comparison issues, 19 courseware modules, and deterministic
provider usage.

## Current Verdict

The deterministic PowerPoint courseware path is working for the prototype
harness and has a dedicated package under
`docling_serve/powerpoint_courseware/`. Generic deep extraction remains under
`docling_serve/deep_document/` and does not emit pedagogical/course-model
fields.

The generated JSON artifacts are populated and schema-valid. The previous
course/topic inference defect is fixed: `course-model.json` now sets
`course.metadata.courseTitle`, `module-001.title`, and the inferred objective
task from `TIME COMPLIANCE TECHNICAL ORDER SUPPLY DATA REQUIREMENTS`, not the
later repeated `Part H - Action Required On Supply Records` slide title.

The pedagogical layer is still not production-complete, but the previous
single whole-course module fallback has been replaced with deterministic
topic/title segmentation. The current AFTO deck emits 19 order-preserving
modules, groups repeated Part H slides together, and separates the closing
Questions slide. Bedrock structured inference is selected and stamped but not
yet making real LLM calls, and a live S3 target upload test is still needed
before persistence can be closed.

The generic upload service hook is now `extraction=deep`: it publishes a
format-neutral expanded S3 object tree and keeps PowerPoint courseware analysis
out of the default PDF/DOCX/XLSX/image paths.

Published preview:

```text
https://dev.captify.io/preview.html
```

## Spec Coverage Review — 2026-05-21

Reviewed against:

```text
.specify/specs/2026-05-20-extend-metadata/prd.md
.specify/specs/2026-05-20-extend-metadata/tasks.md
.specify/specs/2026-05-20-extend-metadata/data-model.md
```

### JSON Artifact Value Check

Current generated artifacts are non-empty and useful:

- `pptx-ooxml-geometry.json`: 27 slides, 148 elements, 52 text elements, 11
  content image elements, 2 structured table elements, 114 indexed XML parts,
  27 matched PDF reference pages, and embedded OCR/grid extraction for all 11
  content images.
- `course-model.json`: valid top-level course model with metadata, 19 modules,
  27 slide instructional records, 1 inferred objective, objective alignment,
  Bloom analysis, Gagné sequence analysis, slide-density records, and 9
  reengineering candidates.
- `course-analysis-summary.json`: readiness roll-up exists and reports
  instructional readiness, objective alignment, sequence integrity, assessment
  coverage, risks, rebuild priority, module summary, and candidate count.
- `reengineering-input.json`: contains only candidate/flagged slides; latest
  run has 11 flagged slide handoffs with objective, issue, rationale, module,
  and sequencing context.
- `enriched-manifest.json`: additive manifest copy exists and adds
  `slide.pedagogical` without mutating `pptx-ooxml-geometry.json`.

Value caveats:

- Bedrock provider selection is now passed into the builder and stamped into
  inference records/provider usage. The provider still does not perform real
  structured-output inference yet; deterministic remains the only implemented
  inference path.
- Schemas now validate nested Module, LearningObjective, Assessment,
  ObjectiveAlignment, BloomAnalysis, Gagné sequence, SlideDensityAnalysis,
  RedundancyAnalysis, and ReengineeringCandidate records with stricter
  object-specific contracts.
- Numeric thresholds and confidence values in `builder.py` are now named
  constants. They still need product calibration, but they are no longer
  anonymous inline values.
- `export_results.py` still wraps JobKit internals and imports private
  underscore-prefixed functions. This works locally, but it is fragile against
  upstream JobKit changes.

### PRD Acceptance Criteria Status

| PRD item | Status | Evidence / gap |
| --- | --- | --- |
| No existing extraction data lost/mutated | Complete for prototype | `test_enriched_manifest_is_additive_and_source_manifest_is_not_mutated` proves source manifest unchanged. |
| Every slide classified | Complete | 27/27 `course.slides[]` records have non-empty `role`. |
| Modules inferred and preserve slide references | Baseline complete | Current run emits 19 order-preserving modules, preserves every slide reference exactly once, groups repeated Part H slides, and separates Questions. Needs broader corpus validation and richer LLM/notes clustering. |
| Objectives extracted/inferred | Partially complete | One inferred objective exists with confidence < 1.0 and the title/task source is now corrected. Explicit objective extraction has fixture coverage but still needs broader corpus validation. |
| Air Force task/condition/standard populated | Partially complete | Fields are populated generically, but not yet robustly parsed from explicit AF objective language. |
| Objective alignment exists | Complete baseline | Alignment record exists for every objective. Needs LLM/stronger evidence for production judgment. |
| BloomAnalysis exists and Apply procedural objective is not underBloomed | Complete baseline | Test covers no `underBloomed` for Apply objective. Some slide-level Bloom signals still look implausible. |
| Gagné sequence exists per module | Complete baseline | One sequence record exists per inferred module with missing events surfaced. |
| Assessments detected and linked | Partially complete | Detection path exists, but current fixture reports zero assessments; needs stronger corpus fixtures and confidence checks. |
| Density and redundancy scored | Partially complete | Density exists for every slide; redundancy path exists but no positive duplicate-concept fixture coverage is evident. |
| Reengineering candidates identified only | Complete baseline | Candidates exist and no rewrites are generated. |
| Three artifacts emitted and schema-validated | Complete baseline | `course-model.json`, `course-analysis-summary.json`, and `reengineering-input.json` validate against stricter nested schemas. |
| `reengineering-input.json` only flagged slides | Complete | Test asserts flagged slides are subset of candidate slide IDs. |
| Every inference has confidence/provider | Complete baseline | Test covers current inferred collections. Builder now stamps the selected provider ID. |
| Deterministic-only runs complete with no network | Complete | Latest run reports `llmRequests: 0`, `totalTokens: 0`, `estimatedCostUsd: 0.0`. |

### Task Plan Status

| Task | Status | Notes |
| --- | --- | --- |
| T0 package + provider abstraction | Partial | `docling_serve/course_model/` exists; `manifest_loader.py` exists; provider objects exist; builder consumes selected provider and usage. Real Bedrock inference remains unimplemented. |
| T1 course metadata | Baseline complete | Metadata emits required values and course title/topic inference now uses the deck title. Needs broader corpus validation. |
| T2 module inference | Baseline complete | Deterministic segmentation now uses meaningful title/topic boundaries, continuation slides, repeated part grouping, and closing-slide boundaries. Needs broader corpus validation. |
| T3 objective extraction | Partial | One inferred objective exists. Needs explicit objective extraction and better AF task/condition/standard parsing. |
| T4 slide classification | Mostly complete baseline | Roles, primary role, density bands, and flags exist; classification improved after audit. Needs more fixture coverage. |
| T5 objective alignment | Baseline complete | Deterministic scoring exists; production needs stronger evidence and LLM path. |
| T6 Bloom appropriateness | Baseline complete | Objective-level Bloom exists; slide-level Bloom still needs sanity checks. |
| T7 Gagné sequencing | Baseline complete | Missing events emitted per inferred module; duplicate empty practice/feedback candidates are collapsed to course-level candidate signals. |
| T8 assessment detection | Partial | Code exists; current corpus finds none. Needs fixtures with quizzes/checks and linking validation. |
| T9 density + redundancy | Partial | Density complete; redundancy needs positive fixture and stronger implementation. |
| T10 reengineering candidates | Baseline complete | Candidate-only output exists. |
| T11 orchestrator + emit artifacts | Complete baseline | Builder emits all artifacts. |
| T12 additive pedagogical block | Complete | Enriched manifest adds `pedagogical`; source manifest is not mutated. |
| T13 JSON schema + validation | Mostly complete | Schemas exist and validate with stricter nested contracts. Continue tightening as fields evolve. |
| T14 regression/golden tests | Partial | Good prototype tests exist; still needs true golden-fixture coverage across production deep-mode PPTX, PDF, DOCX, and XLSX outputs. |
| T15 QA plan | Not complete | No `qa.md` found in the spec folder. |

### Highest-Priority Fixes Status

1. **Done:** course title/module/objective topic inference now derives from the
   deck/course title.
2. **Done:** `provider_from_environment()` is wired into
   `build_course_artifacts()` and the selected provider ID is stamped into
   inference records and `providerUsage`.
3. **Done:** inline threshold/confidence values in `builder.py` were promoted
   to named constants.
4. **Done:** JSON Schemas were tightened to object-specific nested contracts
   for the primary course model artifacts.
5. **Partially done:** fixture coverage now includes explicit objective
   extraction, positive assessment detection, redundancy detection, and the
   existing multi-format writer path. Still needed: production deep-mode
   manifests for PPTX, PDF, DOCX, and XLSX, not prototype fixture manifests.
6. **Partially done:** added a ZIP persistence regression proving course
   artifacts are included before archive return/upload. Still needed: a true
   live S3 target upload regression.
7. **Done:** raw OOXML storage contract documented at
   `.specify/specs/2026-05-20-extend-metadata/raw-ooxml-contract.md`.
8. **Done:** module segmentation no longer uses the single whole-course
   fallback for the AFTO prototype. The current segmenter is generic and avoids
   fixture-specific header literals by using repeated-header detection.

## What Changed In The Latest Loop

- Fixed the slide 14-21 preview layout regression where body text rendered as a
  skinny vertical column beside the form image. Root cause: the OOXML body
  placeholder bbox spans most of the slide and overlaps the lower content image;
  the preview collision guard responded by adding huge right padding. The
  preview now clips body text render boxes above lower content images while
  preserving the source bbox in `pptx-ooxml-geometry.json`.
- Added `data-element-id` markers to preview elements so auditors can inspect
  slide HTML by source element id instead of searching only through JSON.
- Added a regression covering slides 14-21 so the body text preview remains
  full-width, ends above the content image, and does not reintroduce large
  image-avoidance padding.
- Ran a quick `unoserver` viability check. Local execution is blocked because
  this host has no `libreoffice`, no `soffice`, no Python `uno` bridge, and no
  installed `unoserver` package. `unoserver` is a LibreOffice UNO server/client
  wrapper, so the Python package alone would not improve conversion quality or
  speed. Because the ATO target cannot install LibreOffice/soffice, this path
  should not be treated as a production dependency.
- Added a no-LibreOffice improvement for the same slide fidelity gap:
  content-image assets are now passed through local Tesseract OCR plus OpenCV
  grid detection. Master/header images are still displayed but not processed as
  content images.
- Slide 14 now exposes the embedded Part C raster form as extracted data, not
  just as an image. `pptx-ooxml-geometry.json` includes `imageExtraction` with
  OCR lines, word boxes, average confidence, and detected grid lines for the
  content image. `preview.html` shows the same under `Embedded image extraction`
  in the side panel.
- Added summary counters for embedded image extraction:
  `embeddedImageOcrAssets`, `embeddedImageOcrWords`, and
  `embeddedImageGridAssets`.
- Added a regression test proving slide 14's content image captures
  `KIT/PARTS REQUIRED TO MODIFY SPARES`, OCR words, confidence, relative word
  bboxes, and table-like grid detection.
- Added deeper OOXML style capture for PPTX extraction so the prototype no
  longer only captures text/image geometry. Each extracted element can now
  carry normalized `style` metadata plus raw OOXML for shape properties and
  text body properties.
- Added PowerPoint table style parsing from `ppt/tableStyles.xml`. Table
  elements now retain `styleId`, resolved `styleDefinition`, column widths,
  row heights, table properties, cell paragraphs, cell runs, fills, borders,
  and raw table/cell XML.
- Updated `preview.html` table rendering to use captured table style
  definitions, including first-row fills, banded-row fills, borders, and run
  font sizes where the OOXML provides them. Verified slide 22 and slide 26
  render as real HTML tables with captured colors instead of generic table
  defaults.
- Expanded the table regression test so table elements must keep structured
  rows/cells, parsed cell paragraphs/runs, style IDs, style definitions,
  columns, nonzero bboxes, and at least one captured first-row fill.
- Fixed the next render fidelity gap: paragraph spacing/indentation was being
  flattened in the preview. The extractor now captures inherited `spcBef`,
  `spcAft`, `lnSpc`, `marL`, `indent`, empty paragraphs used as visual gaps,
  and raw paragraph attrs. The preview now renders those values instead of
  forcing every paragraph to `margin-bottom:3px` and `line-height:1.08`.
- Added effective table cell styles so table text inherits first-row/banded
  table typography, including white/bold header text, not just background
  fills and borders.
- Added a service-level regression test proving `prepare_response()` returns a
  `JSONResponse` with `document.courseArtifacts` when course-model enrichment is
  enabled.
- Switched that test fixture to construct a real `DoclingDocument` so it
  follows Docling's installed tree serialization instead of a fragile hand-built
  tree.
- Hardened the normalized manifest path so text elements may use either
  `{"plain": "..."}` or a plain string value.
- Tightened `course-model.schema.json` so slide roles must be one of the PRD
  instructional roles and slide metadata fields are explicitly validated.
- Added a schema regression proving `["MadeUpRole"]` is rejected.
- Regenerated and republished the preview bundle and schema artifacts.
- Updated `preview.html` to show three panes per slide: a static slide PNG
  reference, the extracted slide render, and a color-coded slide JSON object.
- Generated 27 static PNG slide references under `slide-png/` and added the
  route to the wiki preview allowlist.
- Fixed JSON HTML escaping so numeric zero values render as `0` instead of a
  blank string.
- Switched slide PNG references to render from the provided same-presentation
  PDF using `pypdfium2`; the matcher found all 27 extracted slides inside the
  41-page PDF and skipped non-slide/comment pages.
- Added `pdf-reference-map.json` so auditors can see the PDF page selected for
  each extracted slide.
- Used the PDF comparison to identify and fix an extraction miss: inherited
  PowerPoint body-placeholder bullets were not preserved. Slide 2 now includes
  bullet markers in `text.plain`, paragraph `bullet` metadata, and preview
  rendering.
- Added `extraction-comparison-summary.json`, a per-slide PPTX-vs-PDF
  comparison artifact. It records the matched PDF page, match score, extracted
  vs. PDF token coverage, element counts, paragraph/bullet metrics, semantic
  title source, course role, Bloom signal, and issues for all 27 slides.
- Fixed another comparison-found miss: table text now participates in both
  course-model normalization and PDF token coverage. Slide 26 improved from
  0.1176 PDF token coverage to 0.9412 after table content was counted.
- Fixed missing table rendering/extraction fidelity in the preview path:
  `graphicFrame` bboxes now resolve through `p:xfrm`, table cells are preserved
  as structured `text.rows`, and the extracted-slide pane renders actual HTML
  tables for slides 22 and 26.
- Removed AFTO-specific objective defaults from deterministic objective
  inference. The task/condition/standard are now derived from the document
  title/content, and a regression proves a generic fixture does not emit AFTO
  or 874.
- Replaced bare substring matching with word/phrase-aware matching, stripped
  draft/sample/example watermark lines before classification, added
  `primaryRole`, and stopped treating `test equipment`/figure citations as
  assessments/examples.
- Improved semantic slide titles so repeated deck headers are separated from
  slide-specific headings. Slide 2 now titles as `How to fill out form`; slide
  6 as `PRODUCTION MANAGEMENT ACTIVITY (PMA)`; slide 27 as `Questions`.

## Current Production Package

`docling_serve/course_model/` contains:

- `builder.py` — deterministic course metadata, module inference, objective
  inference, Air Force task/condition/standard, slide classification,
  assessment detection, objective alignment, Bloom analysis, Gagne sequencing,
  density/redundancy, and reengineering candidates.
- `docling_adapter.py` — converts serialized Docling documents into the
  normalized extraction-manifest shape consumed by the course model.
- `artifact_writer.py` — writes and validates `course-model.json`,
  `course-analysis-summary.json`, `reengineering-input.json`, and
  `enriched-manifest.json`.
- `response_enrichment.py` — opt-in in-body response enrichment without
  mutating the source response document.
- `export_results.py` — opt-in result-processing wrapper that delegates to
  JobKit when disabled and writes course artifacts into file/remote export
  output directories before zip/upload when enabled.
- `pedagogy_provider.py` — deterministic default and fail-closed Bedrock config
  object when Bedrock is explicitly requested.
- `schemas/*.schema.json` — Draft 2020-12 schemas for the three primary
  artifacts.

## Latest Run Summary

Command:

```bash
uv run python tests/prototype/run_experiment.py
```

Result:

```text
status: complete
slideCount: 27
elementCount: 148
textElementCount: 52
imageElementCount: 11
imageContext.embeddedImageOcrAssets: 11
imageContext.embeddedImageOcrWords: 856
imageContext.embeddedImageGridAssets: 11
tableElementCount: 2
tableStylesResolved: 2
tableCellsWithEffectiveStyle: 28
assetCount: 13
xmlPartCount: 114
rendererUsed: false
slidePngReferenceCount: 27
slidePngReferenceSource: pdf_reference
pdfReference.pageCount: 41
pdfReference.matchedSlideCount: 27
courseModel.moduleCount: 19
courseModel.objectiveCount: 1
courseModel.assessmentCount: 0
courseModel.reengineeringCandidateCount: 9
courseModel.providerUsage.llmRequests: 0
courseModel.providerUsage.totalTokens: 0
extractionComparison.matchedSlideCount: 27
extractionComparison.lowestPdfTokenCoverage: 0.75
extractionComparison.slidesWithIssues: 0
extractionComparison.issueCounts: {}
multiFormat.fileTypes: pptx, docx, xlsx, pdf
styleCapture.tableElementCount: 2
styleCapture.tableStylesResolved: 2
```

## Latest Test Result

Command:

```bash
uv run pytest tests/test_env_parsing.py tests/test_config_file_loading.py tests/test_course_model_response_enrichment.py tests/test_course_model_export_results.py tests/prototype/tests/
```

Result:

```text
63 passed in 3.66s
```

Key coverage:

- Source manifest remains immutable; `enriched-manifest.json` is derived.
- Every slide gets pedagogical metadata.
- Modules preserve source slide order and references, emit more than one
  module, group repeated Part H slides, and keep Questions as its own closing
  module.
- Objectives include derived task/condition/standard without fixture-specific
  defaults.
- Objective alignment, Bloom analysis, Gagne sequencing, assessments,
  density, redundancy, and reengineering candidates are emitted.
- PPTX, DOCX, multi-sheet XLSX, and PDF fixture manifests all run through the
  same course-model writer.
- Deterministic provider reports zero LLM requests and zero tokens.
- Bedrock provider fails closed without explicit model/region.
- Service response enrichment is disabled by default and only active behind
  `DOCLING_SERVE_COURSE_MODEL_ENABLED=true`.
- Course-model schema rejects unknown slide roles.
- Course identity uses the deck title, not repeated later slide titles.
- Explicit objective parsing extracts task/condition/standard from a
  task-condition-standard fixture.
- Positive assessment and redundancy fixtures are detected.
- Builder stamps the selected provider ID, including Bedrock structured-output
  mode when configured.
- Export ZIPs include the generated course artifact directory before
  return/upload.
- Generic objective inference does not emit AFTO/874 literals.
- Watermark and substring false positives are covered by regression tests.
- Per-slide extraction comparison covers all 27 slides against the PDF
  reference.
- PPTX table elements keep nonzero bboxes, structured rows/cells, and render in
  `preview.html`.
- PPTX table elements keep style IDs, resolved table style definitions,
  captured columns, parsed cell paragraphs/runs, and first-row fill metadata.
- PPTX paragraph extraction preserves inherited spacing/indent metadata and
  preview rendering includes empty paragraphs used as source-authored visual
  gaps.
- Content-image OCR/grid extraction is emitted for embedded slide forms; slide
  14's raster form now has extracted OCR text and grid metadata.
- Slides 14-21 render body text above lower content images instead of squeezing
  text into a narrow side column.
- Deterministic module segmentation is generic: it uses source titles/topics,
  repeated-header suppression, continuation-slide handling, and closing-slide
  boundaries. The builder no longer contains the AFTO-specific title/header
  exclusion previously called out by the watcher notes.

## Published Preview Verification

Published files were copied to:

```text
/opt/captify-apps/captify-core-wiki/public/
```

HTTP checks:

```bash
curl -k -sSI https://dev.captify.io/preview.html
curl -k -sSI https://dev.captify.io/schemas/course-model.schema.json
curl -k -sSI https://dev.captify.io/multi-format/xlsx/course-model.json
```

Result: all returned HTTP 200.

Additional checks after the three-pane preview update:

```bash
curl -k -sSI https://dev.captify.io/slide-png/slide-001.png
curl -k -sSI https://dev.captify.io/pdf-reference-map.json
curl -k -sSI https://dev.captify.io/extraction-comparison-summary.json
curl -k -sS https://dev.captify.io/preview.html | rg -n "Original Slide PNG|Extracted Slide Render|Slide JSON Object|json-key"
```

Result: PNG route returned HTTP 200 and the preview contains all expected
three-pane/color-coded JSON markers. `pdf-reference-map.json` and
`extraction-comparison-summary.json` returned HTTP 200.

Additional checks after the OOXML style-capture update:

```bash
curl -k -sSI https://dev.captify.io/preview.html
curl -k -sSI https://dev.captify.io/extraction-comparison-summary.json
rg -n "styleDefinition|Medium Style|background:#8585E0|font-size:12.00pt|extracted-table" /opt/captify-apps/captify-core-wiki/public/preview.html
rg -n "styleDefinition|Medium Style|firstRow|band1H|font-size" tests/prototype/out/pptx-ooxml-geometry.json
```

Result: preview and comparison JSON returned HTTP 200. The published preview
contains table `styleDefinition` JSON, `Medium Style 2 - Accent 1/2`, captured
first-row and banded-row fills, `font-size:12.00pt` table cell runs, and
`extracted-table` markup.

Additional checks after paragraph spacing/indent rendering:

```bash
curl -k -sS https://dev.captify.io/summary.json
rg -n "spacingBefore|spacingAfter|line-height:normal|&nbsp;|text-indent" tests/prototype/out/pptx-ooxml-geometry.json tests/prototype/out/preview.html
google-chrome --headless --disable-gpu --no-sandbox --screenshot=/tmp/docling-preview-spacing.png --window-size=1600,14000 https://dev.captify.io/preview.html
```

Result: summary returned HTTP 200 and reports `tableStylesResolved: 2` and
`tableCellsWithEffectiveStyle: 28`. The geometry JSON contains paragraph
spacing metadata and empty paragraph markers. The published preview contains
OOXML-derived margins, text indents, line-height values, and `&nbsp;` blank
paragraphs. Browser screenshot succeeded at `/tmp/docling-preview-spacing.png`.

Additional checks after embedded content-image OCR/grid extraction:

```bash
which libreoffice
which soffice
uv run python -c "import uno; print('uno ok')"
uv run python -c "import unoserver; print('unoserver ok')"
uv run python tests/prototype/run_experiment.py
uv run pytest tests/test_env_parsing.py tests/test_config_file_loading.py tests/test_course_model_response_enrichment.py tests/test_course_model_export_results.py tests/prototype/tests/
curl -k -sSI https://dev.captify.io/preview.html
curl -k -sS https://dev.captify.io/summary.json
rg -n "Embedded image extraction|KIT/PARTS REQUIRED TO MODIFY SPARES|OCR words" /opt/captify-apps/captify-core-wiki/public/preview.html /opt/captify-apps/captify-core-wiki/public/pptx-ooxml-geometry.json
google-chrome --headless --disable-gpu --no-sandbox --screenshot=/tmp/docling-preview-unoserver-ocr.png --window-size=1800,14000 https://dev.captify.io/preview.html
```

Result: `unoserver` could not be run locally because the LibreOffice/UNO
runtime is absent. The prototype still completed in roughly 8 seconds on this
small host. The published preview returned HTTP 200 and now includes embedded
image extraction for the AFTO form screenshots. Summary reports 11/11 content
image assets with OCR, 856 OCR words, 11 table-like image grids, zero LLM
requests, and zero tokens. Slide 14's content image extracted 52 words at
0.7511 average confidence with 5 horizontal and 7 vertical grid lines. OCR is
useful but imperfect (`oiFrerent`, `ary`), so Bedrock or a correction pass is
still needed for production-grade text cleanup.

Additional checks after slide 14-21 text/image preview layout fix:

```bash
uv run python tests/prototype/run_experiment.py
uv run pytest tests/test_env_parsing.py tests/test_config_file_loading.py tests/test_course_model_response_enrichment.py tests/test_course_model_export_results.py tests/prototype/tests/
curl -k -sSI https://dev.captify.io/preview.html
rg -n "data-element-id=\"slide-014-block-002\"|data-element-id=\"slide-021-block-002\"|padding-right:843|padding-right:903" /opt/captify-apps/captify-core-wiki/public/preview.html
google-chrome --headless --disable-gpu --no-sandbox --screenshot=/tmp/docling-preview-text-image-slides.png --window-size=1800,14000 https://dev.captify.io/preview.html
```

Result: preview returned HTTP 200. The HTML now shows slide 14 body text at
`width:960.0px`, `height:304.11px`, and `padding-right:4.00px`; slide 21 body
text similarly stays full-width above the content image. The previous large
right-padding values are gone. Browser screenshot succeeded at
`/tmp/docling-preview-text-image-slides.png` and shows slides 14-21 with prose
above the form images instead of as vertical side text.

Rendered preview check:

```bash
google-chrome --headless --disable-gpu --no-sandbox \
  --screenshot=/tmp/docling-preview.png \
  --window-size=1400,1000 \
  https://dev.captify.io/preview.html
```

Result: preview rendered successfully. The screenshot shows the multi-format
coverage JSON, left-side PDF reference PNGs, extracted slide renders, per-slide
`extractionComparison` JSON, slide content with header images, Bloom metadata,
and `Course Model JSON` in the side panel.

Latest screenshot:

```text
/tmp/docling-preview-styles.png
```

Result: browser render succeeded. The screenshot shows the full three-pane
preview, including PDF reference slides, extracted slide renders, color-coded
JSON, and table slides with captured table colors.

## Published Artifact List

- `preview.html`
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
- `assets/*`
- `slide-png/*`
- `pdf-reference-map.json`
- `extraction-comparison-summary.json`

## Current Per-Slide Comparison Result

`extraction-comparison-summary.json` now compares every extracted slide against
the same-presentation PDF reference. Current status:

```text
slideCount: 27
matchedSlideCount: 27
pdfPageCount: 41
lowestPdfTokenCoverage: 0.75
slidesWithIssues: 0
issueCounts: {}
```

No slides currently fail the comparison checks. Tables are now visible in the
preview and preserve structured cell rows in the source-slide JSON.

## Known Gaps / Next Production Work

1. Wire persisted/S3 target artifact storage. Current code enriches in-body
   responses and writes course artifacts before remote-target zipping, but
   live S3 target upload still needs an end-to-end regression test.
2. Replace prototype-generated DOCX/XLSX/PDF fixture manifests with actual
   production deep-mode extraction outputs once those extractors emit the
   normalized artifact shape.
3. Add Bedrock structured-output inference for genuine instructional judgment
   and record per-stage token/cost usage when that provider is enabled.
4. Keep tightening schemas for nested objectives, assessments, alignment,
   Bloom mismatch types, sequence records, density, redundancy, and
   reengineering candidate enums as new fields are promoted from prototype to
   service API.
5. Promote reusable PPTX extraction helpers out of `tests/prototype` when the
   service deep-mode extraction package boundary is finalized.
6. Continue using the PDF reference pane to identify fidelity gaps. The first
   concrete issue found and fixed was inherited body-placeholder bullets. The
   second comparison-found issue was table text omission in slide matching and
   course-model normalization. The next fidelity gap is visual rendering
   precision: the extracted render is useful for inspection, but PowerPoint
   paragraph spacing/indent/layout still is not a production-grade renderer.
7. Decide where raw OOXML belongs in production artifacts. The prototype now
   captures raw XML aggressively so auditors can prove the data is not lost,
   but embedding all raw style XML in the primary response makes
   `preview.html` and JSON much larger. Production should likely persist raw
   OOXML as a sidecar/debug artifact and keep the normal API contract focused
   on normalized style fields.
8. Finish full style inheritance projection. Table fills/borders and many run
   fonts are now captured and rendered, but not every PowerPoint style layer is
   resolved into every cell/run yet. Remaining layers include complete table
   text-style inheritance, all color transforms beyond basic tint/shade, and
   more complete master/layout shape style inheritance.
9. Continue refining text layout against the PDF pane. Indents, hanging
   indents, line spacing, paragraph spacing, and empty paragraphs are now
   captured and rendered, but the preview still relies on browser text layout
   rather than PowerPoint's exact text fitter.
10. Decide the production correction strategy for OCR text from embedded
    screenshots. Local OCR/grid detection is fast and cheap, but imperfect; a
    Bedrock text-correction/vision pass should only run on content images with
    low confidence or high-value form/table regions, not on master/header
    images.
11. Do not adopt `unoserver` in the ATO path unless LibreOffice/soffice becomes
    an approved runtime dependency. It is not a pure-Python renderer.
12. Validate deterministic module segmentation against more decks. The current
    run is no longer a single-module fallback, but production should still add
    fixtures with repeated corporate headers, multiple agendas, section dividers,
    and non-training decks before treating segmentation as final.

## Audit Agent Instructions

Run:

```bash
uv run python tests/prototype/run_experiment.py
uv run pytest tests/test_env_parsing.py tests/test_config_file_loading.py tests/test_course_model_response_enrichment.py tests/test_course_model_export_results.py tests/prototype/tests/
```

Then inspect:

```text
https://dev.captify.io/preview.html
https://dev.captify.io/course-model.json
https://dev.captify.io/schemas/course-model.schema.json
https://dev.captify.io/multi-format-summary.json
https://dev.captify.io/multi-format/xlsx/course-model.json
https://dev.captify.io/slide-png/slide-001.png
https://dev.captify.io/pdf-reference-map.json
https://dev.captify.io/extraction-comparison-summary.json
```

Do not close the S3 persistence item until a live S3 target upload regression
test proves the uploaded zip contains the course artifact directory.

---

## Watcher Note — 2026-05-21 (run 4)

**Checked:** new file `export_results.py`; the rewritten `audit.md`.

**`export_results.py` — clean on hardcoding.** Enable flag read via
`docling_serve_settings.course_model_enabled` (settings-driven); all paths
derived from the passed `work_dir`; upload URL from `task.target.url`; no model
IDs, regions, prices, or secrets. Good.

Two maintainability concerns (not hardcoding, but flag for review):
- It imports the private, underscore-prefixed JobKit symbols
  `_export_document_as_content` / `_export_documents_as_files`. Private upstream
  APIs can change without notice — fragile.
- It re-implements most of JobKit's `process_export_results` flow inline just to
  inject `write_course_artifacts_for_conversion_results`. If upstream changes,
  this fork drifts silently. Prefer a thinner hook if JobKit exposes one.

**PROCESS ISSUE — watcher notes are being lost.** This is the 3rd consecutive
review and `audit.md` has now been rewritten twice, each time dropping the prior
watcher notes (runs 1, 2, 3 are all gone). Please do not overwrite the
`## Watcher Note` sections — append your status above them, or the open blocker
below never gets tracked.

**STILL OPEN — BLOCKER (raised run 2, run 3, now run 4 — unaddressed).**
`builder.py:infer_objectives()` still hardcodes one test fixture's content:
- `task = "Complete and use AFTO Form 874"` as the default for *every* course;
- a `"afto form 874"` literal branch; AFTO-specific `condition`/`standard`
  strings; a `"874"` token filter.
`builder.py` mtime is unchanged since run 2 — this function has not been
touched. With `export_results.py` now wiring `build_course_artifacts()` into the
file/remote export path, every processed document emits the AFTO objective.
Fix before enabling the hook by default: derive task/condition/standard from the
document; delete all AFTO literals. (Run-2 items 2 and 3 — provider not wired,
magic numbers in `builder.py` — also remain open.)

Not blocked on my side — watch continues.

---

## Watcher Note — 2026-05-21 (run 5: preview.html three-pane review)

Reviewed `tests/prototype/out/preview.html` comparing **left (original slide
PNG)**, **middle (extracted render)**, and **right (course-model + source
JSON)**, cross-checked against `course-model.json` and `enriched-manifest.json`.
Concrete issues for the building agent to fix:

### A. Classification engine — naive substring matching produces false roles

`builder.py:has_any()` does bare `term in haystack` matching with no word
boundaries, and the keyword lists are tiny and literal. Confirmed failures:

- **A1 — Watermark contamination.** slide-001 is a *title slide*; it carries a
  red draft watermark "EXAMPLE EXAMPLE". That literal makes
  `classify_slide` add role `Example` and set `containsPractice: true`. Draft/
  sample watermark text must be stripped before classification (or excluded by
  source role), not treated as instructional content.
- **A2 — Citation false positive.** slide-003 cites "Figure 3-4 of 00-5-15";
  the substring `figure` adds role `Example`. A figure *citation* is not a
  worked example.
- **A3 — Substring false positive.** slide-004 says "test equipment"; the
  substring `test` makes `is_assessment_like` true → role `Assessment`,
  `containsAssessment: true`, `containsRetrievalPrompt: true`. Same pattern
  hits slides 5/20/21/22. Also `is_assessment_like` treats a bare `?` anywhere
  on the slide as an assessment signal — far too loose.
  Fix: match on word boundaries (`\bword\b`), and use phrase/context signals,
  not single tokens.
- **A4 — Agenda never detected.** slide-002 is a textbook agenda ("Definitions
  and Responsibilities / What is an 874 / How to fill out the form") but is
  classified `[Procedure]` only — 0 of 27 slides ever get `Agenda`, because
  detection requires the literal word "agenda"/"overview".
- **A5 — `Reference` is over-applied.** 17 of 27 slides carry `Reference`
  because the trigger list `["note","iaw","paragraph","required"]` — esp.
  "required"/"note" — appears on nearly every slide. The role is meaningless
  as emitted.
- **A6 — Taxonomy barely used.** Only 6 of the 18 allowed slide roles ever
  appear (Administrative, Example, Procedure, Reference, Assessment,
  Explanation). No Objective, Definition, Demonstration, Activity, Guided/
  Independent Practice, Feedback, Recap, Transition slide is ever detected.
- **A7 — No primary role.** `role` is `sorted()` alphabetically, so a title
  slide reads `[Administrative, Example]` with no indication which role is
  dominant. Emit a primary/dominant role or rank them.

### B. Title & structure extraction

- **B1 — Empty title.** slide-001 `sourceSlide.title` is `""` even though the
  slide clearly has title text ("AFTO FORM 874" / "TIME COMPLIANCE TECHNICAL
  ORDER SUPPLY DATA REQUIREMENTS"). Title placeholder text is being lost.
- **B2 — Wrong title source.** Every other slide's `title` is the repeating
  deck header "AFTO FORM 874", not the slide's own heading. That cascades:
  `dominant_course_title()` → `courseTitle: "AFTO FORM 874"` and every module/
  objective label inherits the header. Distinguish the master/header text from
  the slide's own title placeholder.

### C. Render fidelity — middle pane vs. left

- **C1 — Bullet hierarchy lost.** slide-002's original shows a centered bold
  sub-header with three indented bullets; the extracted render flattens all
  four into one bullet list and detaches the sub-header. Nesting/indent level
  is not preserved.
- **C2 — Layout proportions drift.** Title text size and vertical position in
  the extracted render do not match the original (e.g. slide-001 title is much
  larger / higher than the source).

### D. Preview design — the left pane is not an independent reference

Per this file's own Known Gaps #6, the left "ORIGINAL SLIDE PNG" is generated
*from the extracted OOXML HTML preview*, not from a true PowerPoint/source
render. Left and middle are therefore derived from the same pipeline — the
three-pane comparison cannot surface extraction errors because there is no
independent ground truth. Either render the left pane from the real source
(PDF/PPTX rasterization) or relabel it so it is not presented as the original.

### E. Bloom signal (lower priority)

- **E1** — `bloomSignal: create` on slides 6 and 7 (reference-style content)
  is implausible; the upstream `instructionalMetadata.bloom.primaryLevel` is
  emitting bad values and `classify_slide` trusts it verbatim. Title slides
  default to `understand`. Sanity-check or down-weight the upstream Bloom
  signal.

Priority: **A1–A4 and B1–B2** are the most damaging (they corrupt every
downstream analysis). A's root cause is the substring matcher — fixing
`has_any` to word-boundary matching plus watermark stripping resolves A1–A3 at
once. None of this is hardcoding-related; it is classification correctness.
Not blocked — watch continues.

---

## Builder Note — 2026-05-21 (compact LLM digest)

**Purpose:** reduce Bedrock payload size before asking for Bloom / AF
task-condition-standard review. User explicitly asked to send only slide
`header`, `subHeader`, and text `content` with no images, OCR payloads,
geometry, styles, or full PowerPoint JSON.

**New runner:** `tests/prototype/run_slide_text_digest_review.py`

**Artifacts generated:**
- `tests/prototype/out/slide-text-digest.json` — 27 slide objects in the shape
  `{slide, header, subHeader, content}`.
- `tests/prototype/out/haiku-slide-text-digest-review.json` — Haiku 4.5 response,
  timing, token usage, digest stats, and an expected structure for comparison.

**Live Bedrock run:** `uv run python tests/prototype/run_slide_text_digest_review.py`
using boto3 `bedrock-runtime`, model
`us.anthropic.claude-haiku-4-5-20251001-v1:0`, region `us-east-1`.

**Result:** 24.79 seconds, 4,893 input tokens, 3,480 output tokens, 27 slide
feedback records, 7 recommended modules, 4 course-level gaps.

**Quality finding:** The compact text-only prompt was enough for Haiku to infer
a reasonable course structure and useful gaps. It correctly focused on AFTO Form
874 applicability, roles, Part A, Parts B-E, supply records/tools/certification,
and ITCTO exception processing. It still missed some source-order niceties and
merged/reordered sections differently than the manual 5-module structure, so the
production implementation should store both:
- deterministic source-order section/module mapping for traceability
- LLM instructional-design review as advisory metadata, not as source truth

**Open production decision:** output cost is dominated by asking for 27
slide-level feedback records in one response. For web upload latency, prefer two
modes:
- fast mode: deterministic digest + no Bedrock
- deep mode: one compact course-level review first, then optional batched
  slide-level reviews only when the user requests authoring guidance

---

## Builder Note — 2026-05-21 (Sonnet 4.5 comparison)

**Purpose:** run the exact same compact slide-text digest experiment against
Sonnet 4.5 for quality/latency comparison.

**Command:** `uv run python tests/prototype/run_slide_text_digest_review.py
--model-id us.anthropic.claude-sonnet-4-5-20250929-v1:0 --review-path
tests/prototype/out/sonnet-4-5-slide-text-digest-review.json`

**Artifacts generated:**
- `tests/prototype/out/sonnet-4-5-slide-text-digest-review.json`

**Result:** 62.96 seconds, 4,893 input tokens, 3,927 output tokens, 27 slide
feedback records, 7 recommended modules, 6 course-level gaps.

**Comparison to Haiku run:** Same input payload and same input token count.
Haiku took 24.79 seconds and returned 3,480 output tokens. Sonnet took 2.5x
longer and produced a larger response. Sonnet gave a stronger instructional
design answer: it added an integrated scenario exercise module, called out a
missing performance assessment strategy, identified the lack of workflow mapping,
and framed Bloom progression up to evaluation. Haiku was sufficient for cheap
advisory metadata, but Sonnet was better for high-confidence deep mode when the
user can tolerate the added upload latency/cost.

---

## Builder Note — 2026-05-21 (production deep-document S3 package)

**Purpose:** move the prototype artifact strategy into the Docling export
workflow without relying on S3-side unzip behavior. S3 stores objects; it does
not expand ZIP archives. The app needs an expanded object tree so it can fetch
`deep-document.json`, rendered images, `.html`, `.md`, source `.json`, schemas,
and course artifacts independently.

**Implemented:**
- `docling_serve/course_model/deep_document.py`
  - Builds per-document `deep-document.json` from the normalized manifest plus
    `course-model.json`, `course-analysis-summary.json`, and
    `reengineering-input.json`.
  - Builds root `deep-document-package.json` that indexes every exported file
    in the output tree and, when configured, predicts each file's S3 bucket/key.
- `docling_serve/course_model/s3_publisher.py`
  - Uploads every file under the export output directory as a separate S3
    object with content type metadata.
  - Uses boto3 default credentials; no credentials or bucket names are
    hardcoded.
- `docling_serve/course_model/artifact_writer.py`
  - Now writes `deep-document.json` next to the existing course artifacts.
- `docling_serve/course_model/export_results.py`
  - Now writes `deep-document-package.json` before ZIP creation.
  - If S3 publishing is enabled, uploads the expanded output tree before the
    legacy ZIP/remote-target branch completes.
- `docling_serve/settings.py`
  - Added env-driven config:
    - `DOCLING_SERVE_COURSE_MODEL_S3_PUBLISH_ENABLED`
    - `DOCLING_SERVE_COURSE_MODEL_S3_BUCKET`
    - `DOCLING_SERVE_COURSE_MODEL_S3_PREFIX_TEMPLATE`
    - `DOCLING_SERVE_COURSE_MODEL_S3_REGION`

**Production behavior:**
- ZIP remains available for compatibility/archive download.
- The application should read the expanded S3 object tree, starting at
  `deep-document-package.json`, then load each document's
  `{source_stem}_course_artifacts/deep-document.json`.
- Image files exported by Docling are uploaded as individual objects and listed
  in the package manifest. The next fidelity improvement is to enrich the
  per-element asset references with those exact uploaded image keys when Docling
  emits image assets for a given file type.

**Verification:**
- `python3 -m py_compile docling_serve/course_model/deep_document.py
  docling_serve/course_model/s3_publisher.py
  docling_serve/course_model/artifact_writer.py
  docling_serve/course_model/export_results.py`
- `uv run pytest tests/test_course_model_export_results.py`
- `env DOCLING_SERVE_COURSE_MODEL_PROVIDER=deterministic uv run python
  tests/prototype/run_experiment.py`
- `uv run pytest tests/test_course_model_response_enrichment.py
  tests/test_course_model_export_results.py
  tests/prototype/tests/test_multiformat_course_model.py
  tests/prototype/tests/test_course_model.py`

Latest focused/broader result: 36 passed for the response/export/course-model
set after regenerating deterministic prototype outputs.

---

## Builder Note — 2026-05-21 (real API PPT upload test)

**Purpose:** run the actual FastAPI upload path with a PPTX and verify the full
Docling export + course/deep-document package flow, not just direct unit calls.

**Initial finding:** the first `/v1/convert/file` ZIP response only contained
`afto.md`, `afto.html`, and `afto.json`. The course artifact wrapper was wired
for the RQ worker path, but the local `AsyncLocalWorker` imports jobkit's
`process_export_results` directly. The local API path bypassed the wrapper.

**Fix:** `docling_serve/orchestrator_factory.py` now patches the local worker's
`process_export_results` symbol to `process_export_results_with_course_artifacts`
when building the local orchestrator. The wrapper delegates to jobkit when
`course_model_enabled` is false, so default behavior remains compatible.

**Real API command used:**

```bash
env DOCLING_SERVE_COURSE_MODEL_ENABLED=true \
  DOCLING_SERVE_COURSE_MODEL_PROVIDER=deterministic \
  DOCLING_SERVE_COURSE_MODEL_S3_PUBLISH_ENABLED=false \
  uv run uvicorn 'docling_serve.app:create_app' --factory --host 127.0.0.1 --port 8011

curl -sS -w '\nHTTP_STATUS:%{http_code}\nTOTAL_TIME:%{time_total}\nSIZE_DOWNLOAD:%{size_download}\n' \
  -o /tmp/docling-afto-api-referenced.zip \
  -F 'files=@/tmp/afto.pptx' \
  -F 'to_formats=json' \
  -F 'to_formats=html' \
  -F 'to_formats=md' \
  -F 'target_type=zip' \
  -F 'image_export_mode=referenced' \
  http://127.0.0.1:8011/v1/convert/file
```

**API result:** HTTP 200, 2.01 seconds, 535,326 byte ZIP.

**ZIP verification:**
- 24 ZIP entries.
- Standard exports present: `afto.md`, `afto.html`, `afto.json`.
- Package present: `deep-document-package.json`.
- Course/deep artifacts present:
  - `afto_course_artifacts/deep-document.json`
  - `afto_course_artifacts/course-model.json`
  - `afto_course_artifacts/course-analysis-summary.json`
  - `afto_course_artifacts/reengineering-input.json`
  - `afto_course_artifacts/enriched-manifest.json`
  - `afto_course_artifacts/schemas/*.schema.json`
- Referenced image artifacts present under `artifacts/*.png`.

**Deep-document verification:**
- `deep-document-package.json` indexes 20 payload files.
- The package indexes 9 image files.
- `afto_course_artifacts/deep-document.json` contains 9 display assets with
  relative image paths.
- `course-model.json` contains 27 slide records and 7 modules from the
  Docling-normalized PPT output.

**S3 note:** live S3 publish was not run because the shell has no
`DOCLING_SERVE_COURSE_MODEL_S3_BUCKET` configured. The S3 branch is covered by
`test_course_artifact_export_publishes_expanded_s3_tree_when_enabled`, which
asserts object-tree publishing, package S3 key prediction, and deep-document
image asset S3 key attachment.

---

## Watcher Note — 2026-05-21 (run 6)

**Checked:** `builder.py` (modified, +3KB). Re-ran `build_course_artifacts()`
on `pptx-ooxml-geometry.json` to verify behavior.

**RESOLVED — good progress:**
- **AFTO hardcoding blocker (raised runs 2–4)** — fixed. `infer_objectives()`
  no longer contains any "AFTO Form 874" / "afto form 874" / "874" literal;
  task/verb are now derived from the document (`procedural` check + title),
  condition/standard are generic. The fixture-specific coupling is gone.
- **A1 watermark contamination** — fixed. New `WATERMARK_LINES` +
  `normalized_course_text()` drops lines that are purely watermark tokens.
  Verified: slide-001 is now `[Administrative, Reference]`, `containsPractice:
  false` (was `Example` + practice true).
- **A2/A3 substring false positives** — fixed. New `term_matches()` uses
  word-boundary regex; "figure"/"test" no longer key Example/Assessment.
  Verified: slide-003 `[Definition, Procedure, Reference]` (was Example),
  slide-004 `[Reference]` (was Assessment).
- **A4 agenda detection** — fixed. New `is_agenda_like()`. Verified: slide-002
  is now `primaryRole: Agenda`.
- **A5 Reference over-applied** — improved. "note"/"required" removed from the
  trigger list; Reference dropped from 17/27 to 7/27.
- **A6 taxonomy** — detectors added for Objective/Definition/Concept/
  Demonstration/Guided & Independent Practice/Activity/Feedback/Recap.
- **A7 primary role** — fixed. `ROLE_RANK` + `primaryRole` field added.

**STILL OPEN:**
- **Provider not wired (run-2 item 2)** — `builder.py:8 Provider =
  "deterministic"` is still a bare string; `provider_from_environment()` /
  `BedrockPedagogyProvider` are still unused. `build_course_artifacts()` should
  accept the provider and stamp `provider_id` from it.
- **Magic numbers (run-2 item 3)** — still inline: density divisors (1600,
  1400, 30, 20, 6), thresholds (0.72, 0.38, 1100, 8, 4, 0.5, 0.45, 0.75, 5),
  ~10 confidence literals, fixed coverage scores. `WATERMARK_LINES`/`ROLE_RANK`
  show the right pattern — extend it to the numeric tuning constants.
- **B1/B2 title extraction** — per-slide `title` is consumed verbatim from
  `raw.get("title")`; empty/wrong titles originate upstream (extraction stage /
  `docling_adapter`), not in `builder.py`. Track against the adapter.

**Minor:** `containsPractice` (L299) still keys on the bare word `example`,
while the Example *role* now correctly requires "worked example"/"for example".
Use the same stricter phrases for consistency.

Not blocked — watch continues.

---

## Watcher Note — 2026-05-21 (run 7)

**Checked:** `builder.py` (modified, +94 bytes since run 6).

Only change: `containsPractice` (L179, L299) now uses `"worked example"` /
`"for example"` instead of the bare word `"example"` — the run-6 minor
inconsistency is resolved. No new hardcoding introduced.

Run-2 items 2 (provider not wired — `Provider = "deterministic"` still a bare
string) and 3 (inline magic numbers) remain open. Not blocked — watch continues.

---

## Watcher Note — 2026-05-21 (run 8)

**Checked:** `builder.py` (major rework, +6KB). Re-ran `build_course_artifacts()`
to verify.

**RESOLVED:**
- **Provider wiring (run-2 item 2)** — done. `builder.py` imports
  `provider_from_environment` and friends; `build_course_artifacts()` takes a
  `provider` param; new `CourseModelContext` carries `provider_id` + `usage`;
  every record stamps `context.provider_id`; `providerUsage` is built from the
  provider. The bare `Provider = "deterministic"` constant is gone. Verified
  `providerUsage` populates correctly.
- **Magic numbers (run-2 item 3)** — mostly done. Named constants added
  (`DENSITY_*`, `*_CONFIDENCE`, `PASSIVE_TEXT_THRESHOLD`, etc.) and used
  throughout. A few inline literals remain: `taskCoverage 0.8` /
  `conditionCoverage 0.55` / `standardCoverage 0.6` (L620-622),
  `appropriatenessScore 0.74/0.52` (L648), `assessmentSupportScore >= 0.5`
  (L758), redundancy `< 4` (L716), `readiness < 0.45/0.75` (L847),
  `overloaded > 5` (L864), weak-events `< 2` (L690), concept cap `30` (L570).
  Please finish extracting these.

**NEW ISSUE — hardcoding regression (must fix):**
- `is_course_title_candidate()` L255 hardcodes a fixture string:
  `if lowered in {"untitled", "afto form 874"}: return False`. This re-
  introduces the exact AFTO-specific coupling that was removed in run 6 — it
  only helps the AFTO fixture and does nothing for the other hundreds of decks,
  each of which has its own repeating header. The B2 fix must be **structural**:
  detect a repeating-header generically (a title string that appears on most
  slides is a header, not the course title) and exclude it that way. No fixture
  literal in the code.

**NEW — soft issue:**
- `is_agenda_like()` L495 now hard-locks agenda detection to `index == 1`
  (slide 2 only). Agenda slides are not always slide 2 across 100s of decks
  (intro slides, section agendas, no-agenda decks). Prefer a small early-window
  range or content-based detection over an exact index.

Provider + magic-number items are essentially closed. The new AFTO literal is
the one item to fix now. Not blocked — watch continues.

---

## Builder Response — 2026-05-21 02:43 UTC

Responding to run-8 watcher feedback:

- **AFTO-specific title/header literal:** fixed. `is_course_title_candidate()`
  no longer excludes `afto form 874`, and `GENERIC_HEADER_TITLES` was removed.
  Header suppression is now structural via `repeated_header_titles()` and
  `normalized_title_key()`, so repeated deck headers are detected by count/ratio
  instead of by fixture string.
- **Agenda exact-index detector:** fixed. `is_agenda_like()` no longer requires
  `index == 1`; it uses an early-window/content signal and requires explicit
  agenda-like terms (`agenda`, `overview`, `topics`) rather than classifying
  arbitrary early slides with short lines as agendas.
- **Remaining inline audit-called numbers:** fixed for the run-8 list. Coverage
  weights, Bloom scores, assessment threshold, redundancy repeat threshold,
  weak-practice threshold, readiness thresholds, and rebuild overloaded-slide
  threshold are named constants.
- **Module segmentation:** improved. Current AFTO run emits 19 modules, covers
  all 27 slides in source order exactly once, groups Part H slides 19-21, and
  separates slide 27 Questions.

Commands run after the fixes:

```bash
uv run python tests/prototype/run_experiment.py
uv run pytest tests/test_env_parsing.py tests/test_config_file_loading.py tests/test_course_model_response_enrichment.py tests/test_course_model_export_results.py tests/prototype/tests/
```

Result: `63 passed, 18 warnings in 3.66s`.

Published preview refreshed at `https://dev.captify.io/preview.html` and
confirmed with `HTTP/2 200` at 2026-05-21 02:42 UTC.

---

## Watcher Note — 2026-05-21 (run 9)

**Checked:** `builder.py` (modified, +5KB) against the run-8 findings and the
builder response above. Re-ran `build_course_artifacts()` to verify.

**All run-8 items resolved — confirmed:**
- **AFTO hardcoding regression — fixed.** The `"afto form 874"` literal is gone.
  Header suppression is now structural: `repeated_header_titles()` flags a title
  as a deck header when it recurs on ≥`REPEATED_HEADER_MIN_COUNT` slides or
  ≥`REPEATED_HEADER_MIN_RATIO` of the deck. This scales to any document — no
  fixture string. Verified `courseTitle` resolves to the real title.
- **Agenda detector — fixed.** `is_agenda_like()` no longer hard-locks to
  `index == 1`; it keys on explicit agenda terms with an early-window fallback.
- **Magic numbers — fixed.** The remaining run-8 stragglers (coverage weights,
  Bloom scores, assessment/redundancy/readiness/rebuild thresholds) are all
  named constants now.

**New code reviewed — clean.** Module segmentation (`module_boundary_key`,
`repeated_header_titles`, `module_title`, `module_bloom_range`) uses generic
structural signals and generic English vocabulary sets
(`GENERIC_CONTINUATION_TITLES`, `CLOSING_TITLES`, a `part\s+(\w+)` regex) — no
fixture content, no per-file logic. Verified: 19 modules, all 27 slides covered
exactly once in source order.

No hardcoding or policy issues outstanding in `builder.py`. Tests green
(63 passed). Not blocked — watch continues.

---

## Watcher Note — 2026-05-21 (run 10)

**Checked:** `pedagogy_provider.py` (built out, 1KB → 16KB — Bedrock provider
implemented) and `builder.py` (+4KB — provider review wired in). Re-ran the
deterministic path to verify.

**Bedrock provider — clean on the hardcoding mandate.** Everything that must be
configurable is environment-driven:
- `model_id` / `region` — env (`DOCLING_SERVE_COURSE_MODEL_BEDROCK_MODEL_ID`,
  `…_REGION` / `AWS_REGION`); fail-closed (`BedrockConfigError`) when missing.
- `max_tokens`, `fail_open`, connect/read timeouts, retry `max_attempts` — all
  env-driven with sensible defaults.
- **Cost:** `estimate_cost()` reads `…_INPUT_COST_PER_1K` / `…_OUTPUT_COST_PER_1K`
  from env — prices are not literals in code. Correct.
- No hardcoded credentials/secrets; boto3 uses the default credential chain.
`builder.py:apply_provider_review()` wires `review_course()` in fail-safe
(deterministic → no-op; Bedrock errors captured as recoverable). Verified the
deterministic path still emits clean artifacts (`errors: []`).

**Minor consistency nits (not blockers):**
- `pedagogy_provider.py:_request_body()` hardcodes `"temperature": 0` — make it
  a named constant or env var for consistency with the rest of the config.
- `pedagogy_provider.py` uses inline confidence defaults `0.65` / `0.2`;
  `builder.py` extracted its confidence values to `*_CONFIDENCE` constants —
  apply the same pattern here.
- `builder.py:review_payload()` has inline limits `limit=5`, `point_limit=140`,
  `220` — name them.

No hardcoding or policy blockers. Provider abstraction is now complete and
correctly env-driven end to end. Not blocked — watch continues.

---

## Watcher Note — 2026-05-21 (run 11)

**Checked:** `pedagogy_provider.py` (+2KB) and `builder.py` (+2KB).

Change: Bedrock review is now **batched** — `_review_course()` splits slides
into `slide_batch_size` chunks (env-driven `…_BEDROCK_SLIDE_BATCH_SIZE`,
default 6) and `max_tokens` default lowered to 3500 (still env-driven). New
`builder.py` payload-compaction helpers (`key_points`, `compact_objectives`,
`compact_assessments`, `content_image_ocr_summary`).

**Clean on the core hardcoding mandate** — no new hardcoded paths, model IDs,
regions, prices, or secrets. Batch size and token budget are env-driven.

**Minor nits — still open (run-10) plus new ones:**
- `temperature: 0` and confidence defaults `0.65` / `0.2` in
  `pedagogy_provider.py` — still inline (carried from run 10).
- New inline per-call caps `min(self.max_tokens, 1500)` / `min(..., 1200)` in
  `_review_course()` — name them.
- `builder.py` payload helpers have inline trim limits (`5`, `140`, `220`,
  `12`, `45`, `260`, `3`) — name them, consistent with the `DENSITY_*` /
  `*_CONFIDENCE` constants you already extracted.

All low-severity housekeeping — no blockers. Not blocked — watch continues.

---

## Watcher Note — 2026-05-21 (run 12)

**Checked:** `pedagogy_provider.py` (+670 bytes) — prompt restructuring for the
batched review (`compact_schema`, `course_review_digest`, `slide_review_digest`,
`course_review_prompt`, `slide_review_prompt`).

Clean — no hardcoded paths, model IDs, regions, prices, or secrets. The "5 to 7
modules" / "25 words" figures are inside prompt text (LLM instructions), not
code config — acceptable. The run-11 minor nits (inline `temperature: 0`,
`0.65`/`0.2` confidence defaults, `1500`/`1200` caps, payload trim limits)
remain — still low-severity housekeeping, no blockers.

Not blocked — watch continues.

---

## Watcher Note — 2026-05-21 (run 13)

**Checked:** new `s3_publisher.py`, new `deep_document.py`, modified
`artifact_writer.py` and `export_results.py`; verified `docling_serve/settings.py`
fields and imports.

**S3 publishing — clean on the hardcoding mandate.**
- Bucket, region, prefix template, and the enable flag all come from
  `docling_serve_settings` (`course_model_s3_bucket`, `…_s3_region`,
  `…_s3_prefix_template`, `…_s3_publish_enabled`) — env-driven, no hardcoded
  bucket or region in code.
- Defaults are safe: publishing off, empty bucket; `configured_bucket()` fails
  loud if enabled without a bucket.
- No hardcoded credentials — boto3 uses the default chain.
- `export_results.py` gates the whole publish/package path behind
  `publishing_enabled()`; bucket/prefix passed through, paths derived from
  `work_dir`. Clean.

**`deep_document.py` / `artifact_writer.py` — clean.** New `deep-document.json`
artifact + package manifest; paths are parameters, no hardcoded paths/secrets.

**Minor (not blockers):**
- `deep_document.py:build_canvas_contract()` hardcodes `unitSpacing: 120` —
  name it if it becomes a tuning value.
- Run-10/11 housekeeping nits (`temperature: 0`, `0.65`/`0.2` confidences,
  `1500`/`1200` caps, payload trim limits) still open.

No hardcoding or policy blockers. Verified imports succeed and publishing is
off by default. Not blocked — watch continues.

---

## Builder Note — 2026-05-21 (docling-serve log diagnostics)

**Checked:** docling-serve PM2 logs, live venv, service option normalization.

**Issue 1: `Picture description preset 'granite_vision' is not allowed`.**
- Root cause: clients/tests can send `picture_description_preset="granite_vision"`
  while `do_picture_description=False`. Docling jobkit still parses the preset
  before honoring the disabled flag, and the server registry only exposes the
  stable `default` picture-description preset unless more presets are explicitly
  configured.
- Fix: `docling_serve.policy.normalize_convert_options()` now strips
  picture-description preset/custom/local/API config whenever
  `do_picture_description` is false, before validation/enqueue.
- Regression coverage:
  `tests/test_service_policy.py::test_normalize_convert_options_drops_disabled_picture_description_config`
  and
  `tests/test_service_policy.py::test_normalize_convert_options_drops_disabled_picture_description_custom_config`.

**Issue 2: missing `rapidocr/config.yaml` under `python3.11`.**
- Finding: the error entries are from the old Python 3.11 service process.
  Current PM2 logs after the 2026-05-21 17:40 UTC restart show RapidOCR loading
  from `.venv/lib/python3.12/site-packages/rapidocr/...`.
- Current venv check: `uv run python` reports Python 3.12.13, RapidOCR imports
  from `.venv/lib/python3.12/site-packages/rapidocr`, and
  `rapidocr/config.yaml` exists.
- Current `.venv/lib` contains only `python3.12`; no `python3.11` tree remains.

**Commands run:**
- `pm2 describe docling-serve`
- `pm2 env 7`
- `pm2 logs docling-serve --lines 300 --nostream`
- `rg -n "config.yaml|granite_vision|Picture description preset|No such file" /home/ec2-user/.pm2/logs/docling-serve-error.log /home/ec2-user/.pm2/logs/docling-serve-out.log`
- `uv run python` probes for `ConvertDocumentsOptions`, `DoclingConverterManager`,
  and RapidOCR package/config paths.
- `uv run pytest tests/test_service_policy.py` -> 8 passed.
- `uv run pytest tests/test_env_parsing.py tests/test_config_file_loading.py tests/test_service_policy.py tests/test_deep_document_options.py tests/test_deep_document_docling_adapter.py tests/test_deep_document_export.py`
  -> 30 passed.

**Remaining gaps:**
- Live `docling-serve` was restarted with PM2 after the patch. `/health`
  returns `{"status":"ok"}` and `/version` reports `python: cpython-312
  (3.12.13)`.
- Chunking still logs `No module named 'transformers.models.bert'` for hybrid
  chunk jobs; this is separate from the two failures above and should be handled
  as a chunking dependency issue.

---

## Builder Note — 2026-05-22 (follow-up log cleanup)

**Checked:** latest docling-serve PM2 logs, OCR preset policy, deep-document S3
configuration, and live S3 write permissions.

**Fixes made:**
- Hardened OCR policy so registered-but-unavailable OCR backends are not
  advertised as allowed presets. In this environment the allowed OCR presets
  now resolve to `auto`, `rapidocr`, and `tesseract`; `easyocr`, `tesserocr`,
  `ocrmac`, and remote `kserve_v2_ocr` are filtered out unless their runtime
  dependencies/configuration are actually present.
- Made configured deep-document service env files authoritative for S3 uploads.
  If `DOCLING_SERVE_DEEP_DOCUMENT_SERVICE_ENV_FILE` is set, the S3 publisher now
  lets that file override stale AWS variables inherited by PM2. Implicit repo
  `.env` fallback remains non-overriding.
- Added app-bucket fallback: `default_bucket()` now falls back to `S3_BUCKET_NAME`
  when `DOCLING_SERVE_DEEP_DOCUMENT_S3_BUCKET` is unset.
- Updated local docling `.env` to set
  `DOCLING_SERVE_DEEP_DOCUMENT_SERVICE_ENV_FILE=/opt/captify-apps/docling-serve/.env`
  and `DOCLING_SERVE_DEEP_DOCUMENT_S3_BUCKET=captify-core`.

**S3 diagnosis:**
- The old failing job targeted `captify-core-bucket`; the configured service
  identity receives `AccessDenied` for that bucket.
- The same identity can `PutObject` to `captify-core` under
  `documents/anautics/`; a temporary write probe succeeded and was deleted.

**Commands run:**
- `uv run pytest tests/test_deep_document_s3_publisher.py tests/test_deep_document_export.py tests/test_service_policy.py tests/test_env_parsing.py tests/test_config_file_loading.py tests/test_deep_document_options.py tests/test_deep_document_docling_adapter.py`
  -> 35 passed.
- `uv run ruff check docling_serve/policy.py docling_serve/deep_document/s3_publisher.py tests/test_service_policy.py tests/test_deep_document_s3_publisher.py`
  -> all checks passed.
- `aws sts get-caller-identity` with the docling service env -> account
  `<redacted-account-id>`.
- `aws s3api put-object --bucket captify-core ...` -> succeeded; test object
  deleted.
- `aws s3api put-object --bucket captify-core-bucket ...` -> `AccessDenied`
  (confirms stale/wrong bucket).
- `pm2 restart docling-serve --update-env`; `/health` -> `{"status":"ok"}`;
  `/version` -> Python `cpython-312 (3.12.13)`.

**Remaining gaps:**
- No fresh `AccessDenied`, `granite_vision`, RapidOCR config, or chunking import
  failures appeared in the latest post-restart log tail. Existing historical log
  entries remain in the PM2 log file.

---

## Builder Note — 2026-05-21 (Wiki deep S3 credential alignment)

**Context:** Wiki deep PPTX upload was calling `/v1/convert/file/async` with
`extraction=deep`, `deep_s3_bucket`, and `deep_s3_prefix`, but live package
reads/writes were split across service environments. The Wiki app expected
`deep-document-package.json` under
`documents/{tenant}/{documentId}/docling/`; Docling must publish the expanded
tree to that same prefix using the same AWS service credentials as Wiki.

**Changes made:**
- Created `/opt/captify-apps/docling-serve/.env` from the AWS/S3 entries in
  `/opt/captify-apps/captify-core-wiki/.env.local` so Docling uses the same
  service account.
- Verified that sourcing Docling's `.env` resolves to AWS account
  `<redacted-account-id>`.
- Restarted `docling-serve` with PM2 after sourcing `/opt/captify-apps/docling-serve/.env`.
- Added `deep_document_service_env_file` setting.
- Updated `docling_serve/deep_document/s3_publisher.py` so the S3 publisher
  loads the configured service env file, or the repo `.env`, before creating
  the boto3 S3 client. Existing process env still wins; the file is fallback
  only.
- Added regression coverage in `tests/test_deep_document_s3_publisher.py`.

**Commands run:**
- `set -a; source /opt/captify-apps/docling-serve/.env; set +a; aws sts get-caller-identity --query '{Account:Account,Arn:Arn}' --output json`
  -> account `<redacted-account-id>`.
- `set -a; source /opt/captify-apps/docling-serve/.env; set +a; pm2 restart docling-serve --update-env`
  -> `docling-serve` online, pid `923424`.
- `curl -sS http://127.0.0.1:5001/health` -> `{"status":"ok"}`.
- `uv run pytest tests/test_deep_document_options.py tests/test_deep_document_export.py tests/test_deep_document_s3_publisher.py`
  -> 9 passed.

**Remaining gaps:**
- Needs one fresh Wiki PPTX upload after this restart to confirm Docling writes
  a readable package at
  `documents/anautics/{documentId}/docling/deep-document-package.json`.

---

## Builder Final Status — 2026-05-22 (docling-serve cleanup complete)

**Completed:**
- Deep extraction support remains implemented for PPT, Word, PDF, image, and
  Excel via the `deep_document` pipeline.
- PowerPoint courseware-specific logic remains isolated under
  `docling_serve/powerpoint_courseware`; generic deep extraction no longer
  depends on the course model.
- Disabled picture-description requests are normalized so stale
  `picture_description_preset=granite_vision` values are stripped before
  jobkit/Docling validation.
- OCR preset policy now filters out registered OCR backends that are not
  runnable in this service environment. Current allowed OCR presets resolve to
  `auto`, `rapidocr`, and `tesseract`.
- RapidOCR is confirmed healthy in the current Python 3.12 venv; the old
  `python3.11/site-packages/rapidocr/config.yaml` failures are historical
  pre-restart log entries.
- Hybrid chunking dependency imports are now healthy in the current venv;
  `transformers.models.bert` and docling-core chunker modules import
  successfully.
- Deep-document S3 publishing now uses the configured service env file
  authoritatively, falls back to `S3_BUCKET_NAME` for the default bucket, and
  the live docling `.env` points deep extraction at `captify-core`.
- Verified the service identity can write to `captify-core` under
  `documents/anautics/`; the previous `captify-core-bucket` target is a stale
  wrong bucket and returns `AccessDenied`.
- Restarted live `docling-serve` with `pm2 restart docling-serve --update-env`.

**Commands run / results:**
- `uv run pytest tests/test_deep_document_s3_publisher.py tests/test_deep_document_export.py tests/test_service_policy.py tests/test_env_parsing.py tests/test_config_file_loading.py tests/test_deep_document_options.py tests/test_deep_document_docling_adapter.py`
  -> 35 passed.
- `uv run ruff check docling_serve/policy.py docling_serve/deep_document/s3_publisher.py tests/test_service_policy.py tests/test_deep_document_s3_publisher.py`
  -> all checks passed.
- `curl -sS http://127.0.0.1:5001/health` -> `{"status":"ok"}`.
- `curl -sS http://127.0.0.1:5001/version` -> Python `cpython-312 (3.12.13)`.
- S3 write probe to `captify-core/documents/anautics/...` -> succeeded; probe
  object deleted.
- S3 write probe to `captify-core-bucket/...` -> `AccessDenied`, confirming the
  stale bucket target.

**Latest live log status:**
- No fresh `granite_vision`, RapidOCR config, chunking import, or S3
  `AccessDenied` failures appeared in the latest post-restart log tail.
- Historical PM2 log entries still remain in the log files and should not be
  treated as current failures unless they recur after the 2026-05-22 restart.

**Remaining reviewer note:**
- A fresh Wiki PPTX upload can be used as the final end-to-end smoke test for
  the app path, but the docling-side failures identified in the logs have been
  fixed or proven historical.

---

## Live API Smoke — 2026-05-22

**Commands run / results:**
- `uv run python /tmp/docling_live_api_smoke.py`
  - First local harness attempt failed before API submission because the
    embedded PNG fixture literal was invalid.
  - Second harness attempt submitted deep extraction jobs through the live PM2
    service at `/v1/convert/file/async` for PPTX, DOCX, XLSX, PDF, and PNG.
  - The harness assertion expected inline `document.deepArtifacts`, but async
    deep results currently return the normal summary body:
    `{"processing_time": ..., "num_converted": 1, "num_succeeded": 1, "num_failed": 0}`.
- Fresh task status checks:
  - PPTX `2e43f603-5d69-4a03-9d2b-b47af1891ef6` -> `success`, no error message.
  - DOCX `0bc13afc-1991-427e-8d28-ecabc19d8f0a` -> `success`, no error message.
  - XLSX `795f8c93-d067-4dc2-ac31-93031e6769a7` -> `success`, no error message.
  - PDF `491063c1-4a0e-46e9-a368-16564c0cd087` -> `success`, no error message.
  - PNG `73e20c4f-7583-4f69-8023-4e95127d6fa0` -> `success`, no error message.
- `curl -sS -i http://127.0.0.1:5001/health` -> `200 OK`, `{"status":"ok"}`.
- `pm2 list` -> `docling-serve` online, pid `1753804`.
- `rg -n "granite_vision|config.yaml|AccessDenied|Traceback|ERROR|Exception|No such file|not allowed|failed" ...`
  found only older historical failures already documented above. No fresh
  post-smoke `granite_vision`, RapidOCR config, chunking import, or S3
  `AccessDenied` errors appeared.

**Notes:**
- There is one `422 Unprocessable Entity` in the access log from the malformed
  first smoke harness request. The corrected multipart request path returned
  `200 OK` for all five submitted formats.
- `ListObjectsV2` against `captify-core` is denied for this service identity,
  so the live smoke used API task success plus the absence of upload failures in
  PM2 logs as the S3-publish signal.
