# Next Agent Handoff: Prototype + Extend Metadata

Date: 2026-05-21

## Current Active Prototype

Use `tests/prototype`.

The old experiment folders were moved to `tests/trash/experiments/` to keep the
active path clear for the next build agent. They were not deleted.

The prototype is self-contained enough to run without importing from the old
experiment folders:

- runner: `tests/prototype/run_experiment.py`
- tests: `tests/prototype/tests/test_prototype.py`
- helpers: `tests/prototype/deep_document/`
- generated output: `tests/prototype/out/`
- findings: `tests/prototype/prototype-findings.md`

## Verified Commands

These passed after consolidation:

```bash
uv run python tests/prototype/run_experiment.py
uv run pytest tests/prototype/tests/
```

Latest test result:

- 13 tests passed

Latest prototype run:

- status: complete
- slideCount: 27
- elementCount: 148
- textElementCount: 52
- imageElementCount: 11
- tableElementCount: 2
- assetCount: 13
- xmlPartCount: 114
- rendererUsed: false
- reviewRequiredElementCount: 1

The run used deterministic image-caption fallback in this session:

- imageUnderstanding: incomplete
- provider: deterministic_fallback
- imageAssets: 11
- placeholderImageAssets: 11
- usage: null

Earlier Bedrock image-context testing succeeded on the same fixture with 11
content-image requests and about 7,337 total tokens. Bedrock should be treated
as an optional provider path, not a requirement for deterministic tests.

## What The Prototype Does

The prototype parses `.pptx` directly as OOXML. It does not use LibreOffice,
soffice, PDF conversion, screenshots, or commercial/watermarked renderers.

It preserves:

- slide geometry in EMU, inches, and canvas pixels
- text runs, ordered line breaks, fonts, colors, underline, and typography
- speaker notes
- tables
- content/page images
- master/header images for visual rendering
- slide format metadata, including layout, master, theme, title structure, and
  format sources
- extracted XML part index

It emits:

- `tests/prototype/out/pptx-ooxml-geometry.json`
- `tests/prototype/out/canvas-contract.json`
- `tests/prototype/out/pptx-ooxml-geometry.tldr`
- `tests/prototype/out/preview.html`
- `tests/prototype/out/summary.json`
- `tests/prototype/out/assets/*`
- `tests/prototype/out/xml/*`

Header/master images are displayed in the preview but should not be sent to
Bedrock image understanding. Only content/page images should receive image
context.

## Cleanup State

Moved to `tests/trash/experiments/`:

- `experiment1`
- `experiment2`
- `experiment3`
- `experiment4`
- `experiment5`
- `experiment6`
- `experiment7`
- `experiment8`
- `experiment9`

Do not build against files under `tests/trash/experiments/`. They are archival
only and can be deleted later after the production path is stable.

## PRD / Tasks Assessment

The PRD and task plan in this folder are directionally correct. The separation
is right: extraction output remains the source of truth, and the new course
model layer should read that output and emit pedagogical artifacts without
rebuilding, rewriting, or moving slides.

Before implementation, update the specs to match the prototype reality:

1. Replace experiment-specific language with "current deep extraction artifact."
2. Treat `tests/prototype/out/pptx-ooxml-geometry.json` as the current PPTX
   deep-mode artifact.
3. Add a normalized manifest adapter in T0/T0.5. The course model layer should
   consume an internal file-neutral view, not raw PPTX-specific structures.
4. Add `slideFormat` as a required PPTX input field for format-aware
   reengineering.
5. Keep the original extraction artifact immutable. Any `pedagogical` block
   should be emitted in `course-model.json` or a derived/enriched artifact, not
   written back into the source JSON.
6. Move schema/types earlier in the task plan so multiple agents build against
   one contract.
7. Add explicit fixture requirements for PPTX, PDF, DOCX, and multi-sheet XLSX.
8. Add token/cost budget acceptance criteria for every Bedrock-powered stage.
9. Define provider IDs once. Decide whether output records provider family
   (`aws_bedrock`) or exact provider path (`aws_bedrock_vision`,
   `aws_bedrock_structured_output`, etc.).

## Recommended Next Build Plan

1. Update `prd.md`, `tasks.md`, and `data-model.md` with the corrections above.
2. Promote reusable prototype code out of `tests/prototype/deep_document` into
   the production package location chosen by the repo.
3. Keep `tests/prototype` as the regression harness while production code is
   extracted.
4. Build the normalized manifest adapter.
5. Generate canonical fixture manifests for PPTX, PDF, DOCX, and multi-sheet
   XLSX.
6. Build the deterministic course model path first.
7. Add Bedrock structured-output provider after deterministic artifacts and
   schemas are stable.

