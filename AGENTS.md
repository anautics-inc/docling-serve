# Agent Handoff: Docling Deep Extraction

## Current Build Target

The current production-facing deep-extraction package is:

```text
docling_serve/deep_document/
```

Format dispatch lives in `docling_serve/extractors/` (registry order matters;
`PptxExtractor` parses `.ppt`/`.pptx` natively with python-pptx via
`deep_document/pptx_adapter.py`, falling back to Docling structure on failure).
Opt-in enhancers (image context, knowledge graph) live under
`docling_serve/extractors/enhancers/`.

All model calls (vision passes, knowledge-graph extraction) route through the
LiteLLM proxy — see `providers/bedrock.py` and the LiteLLM section of `.env`.
Do not add direct Bedrock/boto3 model calls.

## Removed: PowerPoint courseware prototype (2026-06-11)

`docling_serve/powerpoint_courseware/` (course model, Bloom taxonomy, module
inference, pedagogical review) and its prototype harness
(`tests/prototype/run_experiment.py`, the course-model tests) were removed.
It was a prototype-era instructional-analysis layer that was never wired into
the service. The generic `extraction=deep` path is structural only: no course
model or pedagogical fields are emitted. Historical context lives in
`tests/audit.md` and `.specify/specs/2026-05-20-extend-metadata/`.

Remaining `tests/prototype/` files are archived research history; do not build
against them (same for `tests/trash/experiments/*`).

## Required Verification Loop

For changes to deep extraction or the extraction pipeline, run:

```bash
uv run pytest tests/test_extraction_pipeline.py tests/test_env_parsing.py tests/test_config_file_loading.py tests/test_deep_document_options.py tests/test_deep_document_docling_adapter.py tests/test_deep_document_export.py
```

The service is supervised by pm2 (process name: `docling-serve`);
`pm2 restart docling-serve` is the only sanctioned way to restart it.

## Service Integration

Generic service integration is `extraction=deep` under
`docling_serve/deep_document/`. Captify sends uploads as multipart form data;
outputs publish to S3 as an expanded object tree (see
`docs/captify-s3-docling-upload-flow.md`).

## Engineering Notes That Still Apply

Raw OOXML contract: keep normalized style fields in the primary manifest/API
and persist raw OOXML as a sidecar/debug artifact. Decision documented in:

```text
.specify/specs/2026-05-20-extend-metadata/raw-ooxml-contract.md
```

`unoserver` experiment result: not viable in the ATO environment — it is a
LibreOffice UNO server/client wrapper and there is no `libreoffice`/`soffice`
available, so the Python package alone provides no renderer.
