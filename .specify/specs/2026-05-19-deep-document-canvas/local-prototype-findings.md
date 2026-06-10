# Local Prototype Findings

**Date:** 2026-05-19
**Scope:** local deep extraction smoke test, no S3 persistence

## Inputs

- PPTX: `tests/test_files/1220dd73-5621-458d-950e-657a6738fb14-updated AFTO Form 874 for presentation.pptx`
- PDF: `/opt/captify-apps/captify-core-wiki/public/titan-authorized-user-acceptable-use-policy.pdf`
- DOCX: generated local fixture at `/tmp/deep-doc-prototype/fixtures/training-objectives.docx`
- XLSX: generated local fixture at `/tmp/deep-doc-prototype/fixtures/training-workbook.xlsx`

## Prototype Artifacts

- PPT manifest: `/tmp/deep-doc-prototype/prototype/manifest.json`
- PPT copied media assets: `/tmp/deep-doc-prototype/prototype/assets/pptx-media/`
- Rough PPT render PDF: `/tmp/deep-doc-prototype/rough-render/1220dd73-5621-458d-950e-657a6738fb14-updated AFTO Form 874 for presentation.rough-render.pdf`
- Rough PPT slide PNGs: `/tmp/deep-doc-prototype/rough-render/slides/`
- Rough PPT render manifest: `/tmp/deep-doc-prototype/rough-render/rough-render-manifest.json`
- Turnkey training canvas artifact: `/tmp/deep-doc-prototype/deep-document-training-canvas-artifact.json`
- Pretty-printed artifact: `/tmp/deep-doc-prototype/deep-document-training-canvas-artifact.pretty.json`
- Canvas preview PNG: `/tmp/deep-doc-prototype/deep-document-canvas-preview.png`
- Docling PPT outputs: `/tmp/deep-doc-prototype/docling/`
- Docling PDF outputs: `/tmp/deep-doc-prototype/pdf/`
- Docling DOCX outputs: `/tmp/deep-doc-prototype/docx/`
- Docling XLSX outputs: `/tmp/deep-doc-prototype/xlsx/`
- Prototype script: `/tmp/deep_doc_pptx_probe.py`
- Turnkey artifact generator: `scripts/prototype_deep_document_artifact.py`

## PPTX Findings

Docling command:

```bash
uv run docling \
  --from pptx \
  --to md --to json --to html \
  --image-export-mode referenced \
  --output /tmp/deep-doc-prototype/docling \
  "tests/test_files/1220dd73-5621-458d-950e-657a6738fb14-updated AFTO Form 874 for presentation.pptx"
```

Observed:

- Slide count: 27.
- Docling JSON schema: `DoclingDocument`, version `1.10.0`.
- Docling text items: 222.
- Docling tables: 2.
- Docling pictures: 9.
- Docling referenced image artifacts: 9 PNG files.
- OOXML embedded image relationships found by prototype: 11 PNG files.
- Slides with OOXML speaker notes: 4.
- Docling notes-layer text items: 1.
- Rough render PDF pages: 27.
- Rough render PNGs: 27.

Implications:

- Deep mode must persist Docling JSON. Markdown and HTML are insufficient because speaker notes can be JSON-only or OOXML-only.
- Deep mode should reconcile Docling pictures with OOXML media relationships. They are not always a 1:1 match.
- Full-slide render is still required for visual fidelity. This local environment does not have `libreoffice` or `soffice`, so a rough `python-pptx` + Pillow render was used only to validate slide count/order and produce an external PDF for inspection.
- The rough renderer is not production-fidelity. It draws text boxes and embedded pictures but does not preserve all PowerPoint masters, layouts, effects, fonts, theme styling, charts, SmartArt, or shape fidelity.
- Bloom taxonomy metadata can be attached per slide. The prototype used a deterministic verb heuristic only; production should use Bedrock with a schema-constrained response and evidence.

Prototype Bloom distribution for the AFTO deck:

| Bloom level | Count |
|---|---:|
| understand | 15 |
| remember | 10 |
| apply | 1 |
| create | 1 |
| analyze | 0 |
| evaluate | 0 |

This distribution is not a quality judgment yet because the prototype classifier is intentionally shallow.

## PDF Findings

Docling command:

```bash
uv run docling \
  --from pdf \
  --to md --to json --to html \
  --image-export-mode referenced \
  --output /tmp/deep-doc-prototype/pdf \
  /opt/captify-apps/captify-core-wiki/public/titan-authorized-user-acceptable-use-policy.pdf
```

Observed:

- Pages: 5.
- Text items: 94.
- Tables: 0.
- Pictures: 1.
- Referenced image artifacts: 1 PNG.
- JSON size: about 2 MB.

Production-relevant issue:

- The local run triggered RapidOCR model downloads from ModelScope into the virtualenv. Production ATO deployment must pre-bake or mount required OCR/model artifacts and prevent on-demand external downloads.

Implications:

- PDF deep mode can start with Docling JSON plus referenced images.
- Page render assets and OCR model management must be explicit production requirements.

## DOCX Findings

Generated fixture includes a heading, paragraphs with Bloom verbs, and a table.

Observed:

- Pages: 0 in Docling JSON for this generated DOCX.
- Text items: 3.
- Tables: 1.
- Pictures: 0.
- Markdown captured heading, paragraphs, and table cleanly.

Implications:

- DOCX should use section/heading units, not page units, unless a separate render-to-PDF/page-image step is introduced.
- Comments, footnotes, endnotes, and embedded media still need OOXML adapter coverage.

## XLSX Findings

Generated workbook has two sheets:

- `Objectives`
- `Rubric`

The `Objectives` sheet includes formula `=SUM(C2:C4)` in `D2`.

Observed:

- Docling pages: 2, mapping naturally to sheets.
- Docling tables: 2.
- Markdown rendered one table per sheet.
- The formula string `=SUM(C2:C4)` did not appear in Docling markdown or JSON.
- `openpyxl` can read the formula string with `data_only=False`.

Implications:

- XLSX deep mode needs a workbook adapter. Docling is useful for table-shaped output, but formula preservation must come from an XLSX parser.
- Sheet-level units are the right top-level abstraction.

## Updated Implementation Guidance

1. Keep Docling as the common extraction backbone.
2. Add format adapters:
   - PPTX adapter: OOXML slide order, notes, relationships, embedded media, full-slide render.
   - PDF adapter: page images, extracted figures, OCR/model policy, optional Bedrock/BDA enrichment.
   - DOCX adapter: headings/sections, comments, footnotes/endnotes, embedded media, optional page render.
   - XLSX adapter: sheet order, formulas, displayed values, merged cells, charts/images.
3. Treat Bloom taxonomy as a first-class training metadata module:
   - Use `taxonomy: "bloom_revised"`.
   - Levels: `remember`, `understand`, `apply`, `analyze`, `evaluate`, `create`.
   - Store `level`, `evidence`, `confidence`, and `suggested_improvements`.
   - Use Bedrock for production classification; keep deterministic heuristics for tests/fallbacks only.
4. Do not build S3 last as an afterthought. Even while S3 upload is deferred, keep local artifact paths shaped exactly like future S3 keys.
5. Keep slide rendering behind a renderer abstraction:
   - `rough_python`: local validation only, no ATO package install required.
   - `aspose` or another approved renderer: production-fidelity PPT/PPTX render if licensing and ATO review pass.
   - `none`: structure-only deep mode for environments where rendering is unavailable.

## Turnkey Artifact Shape

The local turnkey artifact is valid JSON with these top-level keys:

- `deepManifest`: extraction units, Docling output references, rough render references, OOXML media references, and source metadata.
- `trainingTaxonomy`: Bloom revised taxonomy definitions, deck-level distribution, and per-slide classifications.
- `canvasProjection`: deterministic Captify/tldraw projection with slide widgets and tldraw commands.
- `bedrockStructuredOutputSchema`: schema that production Bedrock calls should use for slide-level taxonomy classification.

Current AFTO artifact summary:

| Item | Count |
|---|---:|
| slide units | 27 |
| canvas widgets | 27 |
| tldraw commands | 2 |
| dominant Bloom level | understand |
| higher-order slides (`analyze`/`evaluate`/`create`) | 1 |

The current taxonomy provider is `deterministic_fallback`. Production should swap this for Bedrock structured outputs while keeping the same JSON contract.

## Canvas Projection Experiment

Command:

```bash
uv run python scripts/prototype_render_canvas_preview.py \
  --artifact /tmp/deep-doc-prototype/deep-document-training-canvas-artifact.json \
  --out /tmp/deep-doc-prototype/deep-document-canvas-preview.png
```

Observed:

- Preview image size: `1500 x 3360`.
- Rendered widget cards: 27.
- Every slide unit produced a canvas tile.
- Each tile displayed slide number/title, rough slide image, Bloom level, instructional role, notes indicator, confidence, and improvement hint where available.

Conclusion:

- The artifact-to-canvas projection is viable.
- The rough renderer is acceptable for validating slide order and data flow.
- The rough renderer is not acceptable as the final user-facing visual. Production needs an approved renderer, or the canvas should present this as a structured editing view rather than a faithful PowerPoint preview.
