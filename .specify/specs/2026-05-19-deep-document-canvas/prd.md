# PRD: Deep Document Canvas Extraction

**Date:** 2026-05-19
**Owner:** Captify platform team
**Primary repos:** `docling-serve`, `captify-core-wiki`
**Related spec:** `.specify/specs/2026-05-19-configurable-extraction/spec.md`

## Problem Statement

Captify currently treats uploaded documents as files to index. The fast path works: the original file is uploaded to S3, Captify sends the bytes to Docling, Docling returns chunks and markdown, and Captify stores one markdown sidecar back to S3. That supports search and basic preview, but it does not preserve enough structure for users and agents to digitally manage the document online.

The next product outcome is different. Users need to upload a training deck, policy PDF, Word document, or multi-sheet Excel workbook and get a durable, inspectable digital document model: pages/slides/sheets, source text, layout, images, speaker notes, tables, charts, formulas, semantic classifications, and a tldraw canvas representation that an agent can help update and publish from.

PPT/PPTX is the first vertical slice because it exposes the hardest gaps: slide visuals, slide text, embedded images, speaker notes, instructional intent, and republishing. The architecture must still cover PDF, Word/DOCX, and multi-sheet Excel/XLSX without a rewrite.

## Solution

Add two processing modes to Captify document ingestion:

- `fast`: the current behavior. Preserve existing Docling calls, chunks, markdown sidecar, OpenSearch indexing, and status flow.
- `deep`: a new extraction pipeline that produces a versioned Digital Document Artifact in S3, then publishes that artifact into Captify Wiki as a tldraw-backed canvas document.

Deep mode will use Docling as the primary structural extractor, format-specific post-processors for fidelity gaps, and AWS Bedrock for semantic enrichment. For PPT/PPTX, deep mode will extract or generate:

- One logical slide record per slide.
- Full-slide render image for visual fidelity.
- Slide text, tables, images, and layout from Docling JSON plus OOXML post-processing.
- Speaker notes from PPT notes XML or Docling JSON content layers when present.
- Embedded image/media assets as separate S3 objects.
- A training taxonomy classification per slide. Public research did not identify a meaningful "Blook taxonomy"; unless an internal Blook taxonomy is supplied, this PRD assumes Bloom's revised taxonomy with levels `remember`, `understand`, `apply`, `analyze`, `evaluate`, and `create`.
- A tldraw canvas snapshot with one editable slide widget per slide, plus linked asset records for slide images and extracted media.

Deep mode is not just "more markdown." It is a durable artifact contract that downstream tools can load, diff, edit, re-run, and publish.

## User Stories

1. As a Captify user, I want to choose fast processing for ordinary uploads, so that current search ingestion remains quick and cheap.
2. As a Captify user, I want to choose deep processing for a training PPT deck, so that Captify extracts every slide into an editable online workspace.
3. As a Captify user, I want each PPT slide to appear on the canvas as its own widget, so that I can inspect and edit slides one at a time.
4. As a Captify user, I want the canvas slide widget to show the rendered slide image, so that the online view visually matches the uploaded deck.
5. As a Captify user, I want extracted slide text available separately from the slide screenshot, so that I can edit and ask questions about the content.
6. As a Captify user, I want speaker notes preserved, so that training facilitation context is not lost during upload.
7. As a Captify user, I want embedded slide images preserved as addressable assets, so that they can be reused, replaced, captioned, or cited.
8. As a Captify user, I want tables on slides preserved structurally, so that an agent can reason over rows and columns instead of screenshots only.
9. As a Captify user, I want charts detected and described, so that the agent can explain chart meaning and identify whether the visual supports the learning objective.
10. As a training author, I want each slide aligned to a learning taxonomy level, so that I can audit whether the deck moves beyond recall into practice, analysis, and evaluation.
11. As a training author, I want each slide classified by instructional role, so that I can distinguish title, objective, concept, example, exercise, knowledge check, summary, and appendix slides.
12. As a training author, I want the agent to identify missing learning objectives, so that I can improve the deck before publishing.
13. As a training author, I want the agent to suggest slide improvements grounded in the extracted slide data, so that the recommendations are traceable.
14. As a training author, I want to publish an updated deck representation from the canvas, so that reviewed training material can move back into a delivery format.
15. As an agent, I want a manifest that links every text span, note, image, chart, and table back to its slide/page/sheet, so that I can cite and update precisely.
16. As an agent, I want stable artifact IDs, so that I can edit slide 7 image 2 or sheet 3 table 1 without relying on brittle text matching.
17. As an agent, I want both rendered images and extracted structure, so that I can reason visually and semantically.
18. As an agent, I want Bloom taxonomy metadata with evidence and confidence, so that taxonomy alignment can be reviewed instead of treated as fact.
19. As an admin, I want deep extraction to use allow-listed Bedrock models and server-side AWS credentials, so that users cannot inject arbitrary model endpoints or storage credentials.
20. As an admin, I want every deep artifact saved under deterministic S3 prefixes, so that lifecycle, retention, audit, and cleanup are manageable.
21. As an admin, I want deep extraction jobs observable by profile, format, duration, asset count, and failure reason, so that production incidents can be diagnosed.
22. As a developer, I want fast mode byte-compatible with today's markdown path, so that existing Spaces ingestion and search behavior do not regress.
23. As a developer, I want format-specific extraction adapters behind one manifest contract, so that PPT, PDF, DOCX, and XLSX can evolve independently.
24. As a developer, I want the PPT vertical slice to prove the artifact contract before adding every other format, so that the first implementation is narrow but architecturally representative.
25. As a developer, I want a repeatable test corpus, so that Docling upgrades can be validated against known PPT, PDF, DOCX, and XLSX files.
26. As a compliance reviewer, I want the original file, extracted outputs, model choices, effective options, and generated artifacts recorded, so that the system is auditable in an ATO environment.
27. As a compliance reviewer, I want external network calls disabled except approved AWS/Bedrock paths, so that extraction cannot exfiltrate document content.
28. As a PDF user, I want pages, images, tables, OCR text, and bounding boxes preserved, so that a PDF can be managed as a digital document rather than a flat preview.
29. As a Word user, I want headings, paragraphs, tables, images, comments, and footnotes preserved where possible, so that policy documents remain structured.
30. As an Excel user, I want every sheet extracted separately, so that multi-sheet workbooks can be viewed and queried without flattening context.
31. As an Excel user, I want formulas preserved alongside displayed values, so that the agent can understand how workbook numbers are calculated.
32. As an Excel user, I want charts and images from sheets preserved, so that dashboard-like workbooks remain visually meaningful.

## Implementation Decisions

- Keep `fast` as the default mode. If no mode is provided, Captify must use the current Docling integration: chunk async, convert markdown async, save `{originalS3Key}.converted.md`, index chunks.
- Add a `deep` mode to Captify upload/import processing options. Deep mode creates a `DigitalDocumentArtifact` in S3 and then creates or updates a Wiki canvas document from that artifact.
- Treat Docling as the primary extractor, not the only extractor. Docling provides structured JSON, markdown, HTML, text, tables, pictures, page/slide references, and image export modes. Format-specific post-processors fill gaps that Docling does not own.
- Use `to_formats=["md","html","json","text"]` and `image_export_mode="referenced"` for deep extraction. Store outputs in S3 rather than returning large embedded payloads to the client.
- Add a Docling-side or Captify-side artifact store. The preferred production boundary is Captify-owned S3 persistence because Captify already owns tenant, dataset, document, credentials, retention, and audit semantics.
- Store all deep outputs under a deterministic prefix:

```text
{tenantId}/spaces/{datasetName}/{documentId}-{fileName}
{tenantId}/spaces/{datasetName}/{documentId}-{fileName}.converted.md
{tenantId}/spaces/{datasetName}/{documentId}/extractions/{extractionId}/manifest.json
{tenantId}/spaces/{datasetName}/{documentId}/extractions/{extractionId}/docling.json
{tenantId}/spaces/{datasetName}/{documentId}/extractions/{extractionId}/document.md
{tenantId}/spaces/{datasetName}/{documentId}/extractions/{extractionId}/document.html
{tenantId}/spaces/{datasetName}/{documentId}/extractions/{extractionId}/document.txt
{tenantId}/spaces/{datasetName}/{documentId}/extractions/{extractionId}/assets/...
{tenantId}/spaces/{datasetName}/{documentId}/extractions/{extractionId}/canvas/tldraw-snapshot.json
```

- Define `extractionId` as a stable hash of source object version, file SHA-256, extraction mode, Docling versions, Bedrock model IDs, and effective options.
- Define a versioned `DigitalDocumentManifest` with common fields for every format:

```typescript
interface DigitalDocumentManifest {
  schemaVersion: "1.0";
  documentId: string;
  source: {
    bucket: string;
    key: string;
    versionId?: string;
    sha256: string;
    contentType: string;
    originalFileName: string;
  };
  extraction: {
    mode: "deep";
    extractionId: string;
    status: "complete" | "partial" | "failed";
    startedAt: string;
    completedAt?: string;
    doclingServeVersion: string;
    doclingVersion: string;
    effectiveOptions: Record<string, unknown>;
    bedrockModels: Array<{ purpose: string; modelId: string }>;
  };
  outputs: {
    markdownKey?: string;
    htmlKey?: string;
    textKey?: string;
    doclingJsonKey: string;
    tldrawSnapshotKey?: string;
  };
  units: DigitalDocumentUnit[];
  assets: DigitalDocumentAsset[];
  taxonomy?: DigitalDocumentTaxonomy;
  errors: Array<{ stage: string; message: string; recoverable: boolean }>;
}
```

- Define `DigitalDocumentUnit` as the cross-format abstraction:
  - PPT/PPTX: `unitType="slide"`
  - PDF: `unitType="page"`
  - DOCX/Word: `unitType="section"` with optional page render if produced
  - XLSX: `unitType="sheet"` and nested table/range units
- For PPT/PPTX, implement an adapter that combines:
  - Docling JSON for text, tables, pictures, and notes when present.
  - OOXML parsing for slide order, notes XML, relationships, and embedded media.
  - LibreOffice or equivalent headless rendering for full-slide PNG/SVG/PDF images.
  - Bedrock Claude for instructional role and taxonomy classification.
- For PDF, implement an adapter that combines Docling JSON, page images, extracted images, table structure, OCR output, and optional Bedrock/BDA validation for figures and summaries.
- For DOCX/Word, implement an adapter that combines Docling JSON with OOXML extraction for comments, footnotes/endnotes, embedded media, and optional rendered page previews.
- For XLSX, implement an adapter that combines Docling table output with `openpyxl`-style workbook parsing for sheet order, formula strings, displayed values, merged cells, dimensions, hidden sheets, and charts/images where possible.
- Use Bedrock for semantic enrichment, not as the only extraction engine. The first Bedrock tasks are taxonomy alignment, slide role classification, summary generation, and quality checks. Extraction of raw text/layout/assets remains deterministic where possible.
- Support Amazon Bedrock Data Automation as an optional validator/enrichment path for PDFs and office documents when it improves visual grounding, figure crops, confidence scores, or output files. Do not make it mandatory for the first PPT vertical slice.
- Build taxonomy alignment as a replaceable module. Initial public assumption is Bloom's revised taxonomy. If the user supplies an internal Blook taxonomy, only the taxonomy module and prompt/schema should change.
- Taxonomy output must include `level`, `role`, `evidence`, `confidence`, and `suggestedImprovements`; never store a bare label without evidence.
- Publish to tldraw using Captify's existing canvas abstractions. Current code already supports document widgets for PDF/PPTX and a tldraw agent bridge that can create/update shapes. Replace current lightweight PPT XML sniffing with manifest-driven canvas creation.
- tldraw assets should reference S3/proxy URLs, not inline file bytes. tldraw asset records store metadata and source URLs while bytes live in Captify storage.
- Canvas publishing must create one slide/page/sheet widget per unit, plus a manifest-backed inspector panel for extracted text, notes, assets, taxonomy, and agent suggestions.
- Agent editing should update a structured draft artifact first. Regenerating a PPTX, PDF, DOCX, or XLSX is a later publish step and should be treated as a compiler from the edited manifest/canvas state.

## Testing Decisions

- Tests should assert external behavior and artifact contracts, not internal library calls.
- Keep the current fast-mode integration tests and add a regression test that fast upload still saves only the original S3 object and `.converted.md` sidecar.
- Add golden deep-mode fixtures for one PPTX, one PDF, one DOCX, and one multi-sheet XLSX.
- Add a PPTX fixture with visible slide text, embedded image, table, chart-like image, and speaker notes.
- Add an XLSX fixture with multiple sheets, formulas, merged cells, charts/images if practical, hidden sheet, and formatted values.
- Add contract tests for `DigitalDocumentManifest` schema validation.
- Add S3 key generation tests to prove all artifact keys are deterministic and tenant/dataset/document scoped.
- Add Docling request tests to prove deep mode sends `json`, `html`, `md`, `text`, and `referenced` image export, while fast mode sends today's markdown-only options.
- Add PPT adapter tests that verify slide count, slide order, speaker notes, extracted media references, and full-slide render keys.
- Add PDF adapter tests that verify page units, image assets, tables, and source references.
- Add DOCX adapter tests that verify headings/sections, tables, images, and footnotes/comments when fixture support is available.
- Add XLSX adapter tests that verify sheet order, formula preservation, displayed values, and sheet-level canvas units.
- Add Bedrock taxonomy tests with a mocked Bedrock client. Assert prompt input shape, output schema parsing, evidence retention, confidence handling, and safe fallback on model failure.
- Add tldraw publishing tests that verify a manifest produces stable widget IDs, one unit widget per slide/page/sheet, linked asset references, and no duplicate widgets on re-run.
- Add E2E smoke test for the PPT vertical slice: upload fixture in deep mode, wait for artifact manifest, open canvas, verify slide widgets and notes/taxonomy inspector data exist.

## Out of Scope

- Perfect PowerPoint animation and transition preservation.
- Full Microsoft PowerPoint feature parity.
- Editing the original binary PPTX in place.
- Real-time collaborative slide editing beyond the existing Wiki/tldraw collaboration model.
- Training or fine-tuning Bedrock models.
- Letting users provide arbitrary external model endpoints.
- Making Bedrock Data Automation mandatory for all formats.
- Replacing current OpenSearch chunk indexing in fast mode.
- Guaranteeing round-trip visual fidelity for every legacy `.ppt` binary file in the first release. Legacy PPT may be accepted through a conversion path, but PPTX is the primary first slice.

## Further Notes

### Research Summary

- Docling supports PPTX, PDF, DOCX, XLSX, images, and other formats, with exports including markdown, JSON, HTML, split-page HTML, text, DocTags, and VTT. Its image export modes are `placeholder`, `embedded`, and `referenced`; referenced mode exports PNG files and references them from the main document.
- Docling's document model exposes structured items such as text, tables, pictures, captions, provenance, pages, and content layers. In local testing, PPTX speaker notes appeared in JSON as notes-layer text and did not appear in markdown or HTML, which means deep mode must persist JSON.
- AWS Bedrock Data Automation can produce JSON outputs and JSON+files outputs for document processing. JSON+files can include markdown/text/table CSVs and figure crops/rectified images for async jobs. This is a useful optional validator/enricher, especially for PDFs and figure-heavy documents.
- Bedrock with Claude is suitable for structured semantic enrichment over extracted artifacts, including taxonomy alignment and quality checks, provided prompts are schema-constrained and outputs are validated before persistence.
- tldraw stores assets separately from shapes. Asset records hold metadata/source references while actual bytes live in a storage backend. That matches the desired S3 artifact model.
- Captify already has a tldraw agent bridge with `readCanvas`, `createShapes`, `updateShapes`, `insertWidget`, and persistence hooks. Deep document publishing should reuse this command surface instead of hand-editing raw tldraw snapshots.

### First Vertical Slice

The first implementation slice should be:

1. Add deep processing option to Captify upload/import.
2. For PPTX only, run Docling with `md`, `html`, `json`, `text`, `image_export_mode=referenced`.
3. Render every slide to an image.
4. Extract speaker notes and embedded media.
5. Persist a `DigitalDocumentManifest` and all assets to S3.
6. Run Bedrock taxonomy alignment using Bloom's taxonomy unless an internal Blook taxonomy is provided.
7. Publish a Wiki canvas with one slide widget per slide.
8. Let the agent read the slide manifest and propose slide-level edits.

### Format Rollout Order

1. PPTX/PPT training decks.
2. PDF policies and reports.
3. DOCX/Word documents.
4. XLSX/multi-sheet workbooks.

PPTX goes first because it proves the hardest digital-management shape. XLSX goes last because formula/chart/sheet fidelity needs a dedicated workbook adapter rather than relying on document conversion alone.

