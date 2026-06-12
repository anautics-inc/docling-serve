# Configurable Document Extraction — Docling Agent

**Date:** 2026-05-19
**Owner:** Captify platform team
**Targets:** `docling-serve` 1.16.x (docling ≥ 2.88.0, docling-core ≥ 2.45.0)
**Consumers:** `captify-core-wiki` (file widget on canvas), `captify-core-spaces` (KB ingestion), agent tools

---

## 1. Goal

Give users and agents the ability to choose, per file, how much information Docling should extract from PDF, DOCX, XLSX, and PPTX files — from a fast "just give me the text" path to a slow but exhaustive "extract images, classify them, describe them with a VLM, extract chart data, extract formulas as LaTeX" path. Today we use ~5% of Docling's surface (markdown only, no enrichments). This spec defines the configuration model, per-format extraction targets, API contract, and operational requirements so the wiki file widget can render rich source documents and the agent can ask questions grounded in everything in them.

---

## 2. Context

### 2.1 What we have today

Captify currently calls Docling with two parameters:

```python
formData.append("files", blob, filename)
formData.append("to_formats", "md")
# optionally: do_ocr=true, ocr_engine=<name>
```

Result shape (per `lib/spaces/services/docling.service.ts`):
```typescript
interface DoclingConvertResult { markdown: string | null }
```

Verified empirically against `https://docling-staging.afsc.us` on 2026-05-19 using one PDF, one DOCX, one XLSX, and one PPTX. Findings:
- All four formats round-trip cleanly to GFM markdown in 6 ms – 2.1 s (PDF on T4 GPU).
- Server returns 6 representations per document: `md_content`, `json_content`, `html_content`, `text_content`, `doctags_content`, plus `filename`. We discard 5 of them.
- XLSX loses formulas (only rendered values survive).
- PPTX loses all visual layout (animations, positioning, speaker notes).
- PDF inlines images as base64 in markdown — 270 KB PDF → 41 KB markdown, mostly image data.

### 2.2 What Docling actually supports (1.16.x)

Per `docs/usage.md` in this repo, the convert endpoints accept ~40 parameters across:
- **Output:** 8 formats (`md`, `json`, `yaml`, `html`, `html_split_page`, `text`, `doctags`, `vtt`)
- **OCR:** engine selection, language list, presets, custom config, force-OCR
- **PDF backends:** 5 choices (`pypdfium2`, `docling_parse`, `dlparse_v1/v2/v4`)
- **Tables:** structure on/off, fast vs. accurate mode, cell-matching, presets, custom config
- **Image handling:** `image_export_mode` (placeholder | embedded | referenced), `include_images`, `images_scale`
- **Enrichments (opt-in):** `do_code_enrichment`, `do_formula_enrichment`, `do_picture_classification`, `do_chart_extraction`, `do_picture_description`
- **VLM picture description:** preset or custom config, classification allow/deny filters, area threshold, min confidence
- **Whole-document VLM pipeline:** `vlm_pipeline_preset` / `vlm_pipeline_custom_config`
- **Layout model:** preset / custom config
- **Page control:** `page_range`, `document_timeout`, `abort_on_error`, `md_page_break_placeholder`

The picture description model defaults to `smolvlm` (HuggingFace `HuggingFaceTB/SmolVLM-Instruct`). The whole-document VLM defaults to `granite_docling` (IBM). Custom presets and engine-API VLMs (OpenAI-compatible) are supported when `DOCLING_SERVE_ALLOW_CUSTOM_*` env vars are enabled.

### 2.3 Why now

Two consumer pulls converging:
1. **`captify-core-wiki` file widget** (in design) wants to render PDF/DOCX/XLSX/PPTX as canvas widgets with markup. Needs rich extraction so the AI side and the visual side stay coherent.
2. **Agent grounding quality** improves dramatically when picture descriptions, chart numerics, and formula LaTeX are in the index — not just OCR'd text.

---

## 3. Functional Requirements

### R1 — Configurable extraction profiles

The service MUST expose **named extraction profiles** that bundle a coherent set of options, plus a **custom profile** path for callers who want to set individual parameters. Profiles let users and agents say "extract everything" or "be fast" without knowing the parameter zoo.

Required built-in profiles (initial set):

| Profile | Intent | Wall-clock target (GPU) |
|---|---|---|
| `fast` | Text + tables only, no OCR. For known-clean digital docs. | < 1 s for typical files |
| `default` | Today's behavior. Text + tables + auto-OCR. Markdown output. | 2–5 s |
| `rich` | Default + image extraction + picture classification + chart extraction + formula extraction. No VLM. | 10–30 s |
| `rich+vlm` | `rich` + VLM picture description on all images above area threshold. | 30 s – 5 min depending on image count |
| `exhaustive` | Whole-document VLM pipeline (`granite_docling`) + all enrichments. Slow but maximal. | minutes |

Each profile maps to a concrete set of Docling parameters; the mapping lives in code, not config, so version-locking is explicit.

### R2 — Per-format extraction options

Callers MUST be able to override extraction options **per file format** in a single request that contains mixed-format files. Example: "use `rich+vlm` for PDFs and `fast` for XLSX in this batch." This avoids forcing the user to send four separate requests.

### R3 — Image extraction modes

Callers MUST control how extracted images are returned:

| Mode | Behavior | Use case |
|---|---|---|
| `placeholder` | `<image>` token in text outputs | Pure text consumption (RAG, agent context) |
| `embedded` | base64-inlined in markdown/html/json | Self-contained payload (today's default) |
| `referenced` | Images written to scratch, returned as URLs/paths | File widget rendering — avoids re-base64-encoding a 50-page PDF on every page render |

The wiki file widget needs `referenced` mode. The agent ingestion path can stay on `embedded` or move to `placeholder` to shrink context windows.

### R4 — Picture description via VLM

Callers MUST be able to ask Docling to caption images using a vision-language model. Configuration MUST include:

- **Preset selection** (one of the admin-allowed presets — see §6.2)
- **Area threshold** (skip images smaller than X% of page area; default 1%)
- **Classification allow/deny lists** (e.g., describe only charts and diagrams; skip logos and headshots)
- **Min classification confidence** (skip images Docling isn't confident it classified correctly)
- **Custom prompt override** (e.g., "Describe this figure for a federal acquisition officer; focus on data and labels")

If a caller asks for picture description but the file has no images, the request MUST NOT fail — the picture-description fields in the output are simply empty.

### R5 — Other enrichments

Callers MUST be able to opt into each enrichment independently:

- `do_table_structure` (already supported; default `true`)
- `do_code_enrichment` — extract code blocks via OCR on rendered code regions (default off)
- `do_formula_enrichment` — extract math as LaTeX (default off)
- `do_picture_classification` — classify pictures (chart / diagram / photo / logo / other) (default off; required if R4 uses classification filters)
- `do_chart_extraction` — pull numeric series out of chart images (default off; runs after classification)

### R6 — Output format selection

Callers MUST be able to ask for one or more of: `md`, `json`, `html`, `text`, `doctags`. The service MUST return all requested formats in a single response. Defaults stay at `md` for backward compatibility.

For the wiki file widget specifically, the default request will be `["md", "html"]` so the widget can show the high-fidelity HTML view by default and fall back to markdown for editing.

### R7 — Page range and size control

Callers MUST be able to:
- Limit conversion to a page range (`page_range: [start, end]`, 1-indexed)
- Bound per-document wall-clock with `document_timeout` (seconds)
- Receive a partial result with `partial: true` and an `errors[]` field if the timeout hits mid-document, instead of a hard failure

### R8 — Async parity

Every option supported on the synchronous `/v1/convert/file` endpoint MUST also be supported on `/v1/convert/file/async`, with no regressions. The wiki widget will always use async (uploads are user-blocking and may exceed 30 s).

### R9 — Configuration discoverability

The service MUST expose a `GET /v1/profiles` endpoint that returns:
- The list of available profiles with their concrete option sets
- The list of admin-allowed picture description presets
- The list of admin-allowed VLM presets
- Per-format default profile

Agents need to introspect what's available rather than guessing; this endpoint is the source of truth.

### R10 — Audit and reproducibility

Every conversion result MUST include the **effective options** that were used (post-merge of profile + overrides), so a result can be re-created by re-sending the same effective options against the same Docling version. Today this lives in `timings` and `processing_time`; we want a sibling `effective_options` block.

---

## 4. Per-Format Extraction Targets

These are the **content fidelity goals** per format. The configuration in §5 has to be able to express each of these.

### 4.1 PDF

| Item | Required | Notes |
|---|---|---|
| Body text with heading hierarchy | ✅ | Already works |
| Tables with structure | ✅ | `do_table_structure: true, table_mode: accurate` |
| Inline images (extracted + referenced) | ✅ | `include_images: true, image_export_mode: referenced` |
| Per-image classification | optional | `do_picture_classification: true` |
| Per-image VLM caption | optional (per profile) | `do_picture_description: true` |
| Chart numeric extraction | optional | `do_chart_extraction: true` (depends on classification) |
| Math as LaTeX | optional | `do_formula_enrichment: true` |
| Code blocks | optional | `do_code_enrichment: true` |
| OCR'd scanned PDFs | ✅ | `do_ocr: true` (auto); engine via preset |
| Page numbers preserved in output | ✅ | `md_page_break_placeholder: "<!-- page -->"` recommended |
| Layout/coordinates per element | ✅ via `json` output | Needed for click-to-source linking in widget |

### 4.2 DOCX

| Item | Required | Notes |
|---|---|---|
| Body text + headings + bullets | ✅ | Already works; near-perfect |
| Tables | ✅ | Already works |
| Inline images extracted | ✅ | `image_export_mode: referenced` |
| Image captions (VLM) | optional | Same as PDF |
| Footnotes / endnotes | ✅ | Verify Docling supports; if not, file upstream |
| Comments / track changes | should | Surface as JSON sidecar; markdown can't represent natively |
| Embedded objects (Excel charts in Word) | should | Treat as image + run chart extraction if enabled |

### 4.3 XLSX

| Item | Required | Notes |
|---|---|---|
| Per-sheet tables | ✅ | One markdown table per sheet, sheet name as heading |
| Formulas (as strings) | ✅ | Today: lost. Must surface raw formula text alongside computed values in `json` output |
| Cell formatting (currency, percent, dates) | should | Affects display; surface in `json` |
| Charts inside sheets | should | Extract as image; eligible for picture description & chart extraction |
| Cell merges | should | Preserve in JSON; flatten in markdown |
| Hidden sheets and rows | optional | Configurable include/exclude |

> **XLSX caveat:** if Docling does not natively expose formulas (verify against current version), we add a Captify-side post-processor that opens the source XLSX with `openpyxl` and merges formula strings into Docling's `json_content`. Document this as an extension.

### 4.4 PPTX

| Item | Required | Notes |
|---|---|---|
| Per-slide content (one slide → one section) | ✅ | Markdown `# Slide N` headers |
| Slide title and bullet content | ✅ | Already works |
| Tables on slides | ✅ | Already works |
| Speaker notes | ✅ | Must be in `json` output; markdown can append `> notes: ...` |
| Slide images extracted | ✅ | `image_export_mode: referenced` |
| Slide-as-image render (full slide screenshot) | ✅ | Needed by the wiki widget for visual fidelity. May require a sibling endpoint or `image_export_mode: page-render` if supported; otherwise add Captify-side LibreOffice render. |
| VLM caption of slide image | optional | When slide-as-image is generated, caption it |
| Animations and transitions | OUT OF SCOPE | Acknowledged loss |

---

## 5. Configuration Model

The request body for the new behavior. Backward compatible: omitting `extraction_profile` and using only existing form fields gives today's behavior.

```jsonc
{
  // EITHER a profile name OR a full options object OR both (overrides applied to profile)
  "extraction_profile": "rich+vlm",

  // Per-format overrides; if a key matches the file's format, these merge over the profile
  "per_format": {
    "pdf":  { "extraction_profile": "rich+vlm" },
    "xlsx": { "extraction_profile": "fast" },
    "pptx": {
      "extraction_profile": "rich",
      "options": { "do_picture_description": true }
    }
  },

  // Output formats requested
  "to_formats": ["md", "html", "json"],

  // Image handling
  "image_export_mode": "referenced",        // placeholder | embedded | referenced
  "images_scale": 2.0,

  // Picture description tuning (only meaningful if do_picture_description=true)
  "picture_description": {
    "preset": "smolvlm",                    // or admin-allowed alternative
    "area_threshold": 0.01,                 // 1% of page area
    "classification_allow": ["chart", "diagram"],
    "classification_deny":  ["logo"],
    "classification_min_confidence": 0.5,
    "prompt_override": null                 // string or null
  },

  // Pagination / safety
  "page_range": [1, 50],
  "document_timeout": 300,                  // seconds
  "abort_on_error": false,
  "md_page_break_placeholder": "<!-- page -->"
}
```

Files arrive via `multipart/form-data` (today's mechanism). The JSON above is sent as a form field named `extraction` to keep one request flow.

### Profile → option mapping (initial)

```python
PROFILES = {
  "fast": {
    "do_ocr": False, "do_table_structure": True, "table_mode": "fast",
    "include_images": False,
    "to_formats": ["md"],
  },
  "default": {
    "do_ocr": True, "do_table_structure": True, "table_mode": "accurate",
    "include_images": True, "image_export_mode": "embedded",
    "to_formats": ["md"],
  },
  "rich": {
    "do_ocr": True, "do_table_structure": True, "table_mode": "accurate",
    "include_images": True, "image_export_mode": "referenced",
    "do_picture_classification": True,
    "do_chart_extraction": True,
    "do_formula_enrichment": True,
    "do_code_enrichment": True,
    "to_formats": ["md", "html", "json"],
  },
  "rich+vlm": {
    # extends rich
    **PROFILES["rich"],
    "do_picture_description": True,
    "picture_description_preset": "smolvlm",
  },
  "exhaustive": {
    "vlm_pipeline_preset": "granite_docling",
    "do_picture_classification": True,
    "do_picture_description": True,
    "do_chart_extraction": True,
    "do_formula_enrichment": True,
    "do_code_enrichment": True,
    "image_export_mode": "referenced",
    "to_formats": ["md", "html", "json", "doctags"],
  },
}
```

---

## 6. API Contract

### 6.1 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/convert/file` | Synchronous; bounded by `DOCLING_SERVE_MAX_SYNC_WAIT` |
| `POST` | `/v1/convert/file/async` | Async; returns `task_id` |
| `GET` | `/v1/status/poll/{task_id}` | Poll status |
| `GET` | `/v1/result/{task_id}` | Fetch result when status is `success` |
| `GET` | `/v1/profiles` | **NEW** — list profiles, presets, defaults |
| `GET` | `/v1/health` | Existing; required for ALB |

### 6.2 Response shape (additions)

The existing response keeps its shape but adds optional fields:

```jsonc
{
  "document": {
    "filename": "...",
    "md_content": "...",
    "html_content": "...",
    "json_content": { /* DoclingDocument */ },
    "text_content": "...",
    "doctags_content": "...",

    // NEW — when image_export_mode = "referenced"
    "images": [
      {
        "id": "img-001",
        "page": 3,
        "bbox": [x0, y0, x1, y1],
        "url": "https://docling.../scratch/<task>/img-001.png",
        "classification": { "label": "chart", "confidence": 0.92 },
        "description": "Bar chart titled 'Q3 Revenue'..."   // when VLM ran
      }
    ],

    // NEW — when do_chart_extraction = true
    "charts": [
      {
        "image_id": "img-001",
        "kind": "bar",
        "series": [ { "name": "Revenue", "data": [[2024, 1.2], [2025, 1.8]] } ]
      }
    ],

    // NEW — when do_formula_enrichment = true
    "formulas": [ { "page": 2, "latex": "\\sum_{i=0}^n x_i" } ]
  },

  "status": "success",
  "processing_time": 12.3,
  "timings": { /* existing */ },
  "errors": [],

  // NEW — for reproducibility
  "effective_options": { /* exactly what was sent to Docling after profile merge */ }
}
```

### 6.3 Backward compatibility

- Requests that omit `extraction` form field MUST behave exactly as today.
- All NEW response fields MUST be optional and absent (not null) when the corresponding feature wasn't requested.
- The deprecated `ocr_engine` parameter MUST keep working until removed upstream.

---

## 7. Operational Requirements

### 7.1 Hardware

- Production MUST run on a GPU host (T4 or better). Per `captify-core-wiki/docs/runbooks/docling-gpu-fix.md`, CPU-only pushed a 26 KB PDF to ~82 s; on GPU the same path is ~2 s. With enrichments and VLM, CPU-only is unusable.
- `nvidia-container-toolkit` must be installed; container launched with `--gpus all` and `NVIDIA_VISIBLE_DEVICES=all`.

### 7.2 Models

VLM models add disk and memory pressure. The deployment manifest MUST:
- Pre-download models at image build time (`docling-tools models download <model>`).
- Mount a persistent volume at `DOCLING_SERVE_ARTIFACTS_PATH` to avoid re-downloads on restart.
- Initially provision: `smolvlm` (default picture description), `granite_docling` (default VLM pipeline). Other models added as we promote them to admin-allowed presets.

### 7.3 Timeouts and limits

Set environment variables conservatively:

| Variable | Value | Reason |
|---|---|---|
| `DOCLING_SERVE_MAX_DOCUMENT_TIMEOUT` | 1800 (30 min) | Hard ceiling per document |
| `DOCLING_SERVE_MAX_NUM_PAGES` | 500 | Refuse pathological PDFs |
| `DOCLING_SERVE_MAX_FILE_SIZE` | 100 MB | Refuse pathological files |
| `DOCLING_SERVE_MAX_SYNC_WAIT` | 120 | Default; UI uses async anyway |
| `DOCLING_SERVE_SINGLE_USE_RESULTS` | `true` | Don't accumulate scratch |
| `DOCLING_SERVE_RESULT_REMOVAL_DELAY` | 600 | Give the consumer 10 min to fetch |

### 7.4 Picture description batching

When picture description is on, throughput is dominated by VLM inference. Tune:
- `DOCLING_SERVE_OCR_BATCH_SIZE`, `DOCLING_SERVE_LAYOUT_BATCH_SIZE`, `DOCLING_SERVE_TABLE_BATCH_SIZE` per GPU memory.
- `picture_description.batch_size` per request when admins allow custom configs.

Baseline: T4 (16 GB) supports `batch_size=4` for SmolVLM at `scale=1.0`.

### 7.5 Admin allow-lists

By default the service MUST NOT accept arbitrary VLM model URLs (security). The Captify deployment sets:

```env
DOCLING_SERVE_ALLOW_CUSTOM_VLM_CONFIG=false
DOCLING_SERVE_ALLOW_CUSTOM_PICTURE_DESCRIPTION_CONFIG=false
DOCLING_SERVE_ALLOWED_VLM_PRESETS='["granite_docling"]'
DOCLING_SERVE_ALLOWED_PICTURE_DESCRIPTION_PRESETS='["smolvlm"]'
```

New presets are admin-added in the same env, not user-supplied.

### 7.6 Observability

Continue exporting OpenTelemetry per `examples/OTEL.md`. Add per-profile and per-format histograms:
- `docling_extraction_duration_seconds{profile,format}`
- `docling_picture_description_count{model}`
- `docling_chart_extraction_count`
- `docling_failure_total{reason}`

---

## 8. Out of Scope

These are **explicitly not** in this spec; they are tracked elsewhere:

- The Captify-side **file widget on canvas** (separate spec under `captify-core-wiki/.specify/specs/`). This document defines the producer side only.
- A **viewer** for Docling output. Consumers render their own UI from the response.
- **Authentication and authorization** of Docling — handled at the Captify ALB / API gateway, not in this service.
- **PPT animation preservation** — fundamentally unsupported by the PDF/markdown pipeline.
- **Excel formula evaluation** — out of scope; we surface formula strings, not their cross-sheet evaluation logic.
- **Real-time collaboration** on documents — the docling-serve is stateless; collab lives in the consumer (Hocuspocus + tldraw).

---

## 9. Open Questions

These need answers before implementation begins. Each blocks a specific work item.

1. **Does Docling 2.88+ surface XLSX formulas in `json_content`?** If yes, R4.3 needs no Captify-side post-processor. If no, we must own the post-processor and document it. **Owner:** verify by sending an XLSX with formulas to the staging server and inspecting `json_content`. **Blocks:** §4.3.
2. **Does Docling support `page-render` image mode for PPTX (full-slide screenshots)?** If no, we need a sibling pipeline using LibreOffice's `--convert-to png`. **Owner:** test against staging. **Blocks:** §4.4.
3. **What's the latency floor for `rich+vlm` on a 50-page PDF with ~30 figures?** Need a real measurement before we promise UX. **Owner:** benchmark on the staging T4. **Blocks:** §3 profile latency targets.
4. **Should the `extraction` JSON payload be sent as a form field or as a separate JSON body alongside multipart files?** Form field is simpler; JSON body is cleaner. **Owner:** confirm with FastAPI patterns. **Blocks:** §5.
5. **Do we need a Captify-side cache layer keyed on `{file_sha256, effective_options}`?** Avoids re-extracting identical files. **Owner:** measure repeat-extraction rate against existing Spaces ingestion. **Blocks:** §6.
6. **Granite Docling vs SmolVLM** — which is the better default for the federal acquisition / DoD content profile we typically see? **Owner:** A/B test on a curated 20-doc corpus. **Blocks:** §3 `exhaustive` profile choice.

---

## 10. Success Criteria

This spec is "done" when:

- [ ] All 5 built-in profiles return the expected option set when queried via `/v1/profiles`.
- [ ] A request with `extraction_profile: "rich+vlm"` against a multi-page PDF with images returns: text + tables + per-image classifications + per-image VLM descriptions, in a single response.
- [ ] Per-format overrides work: a request with PDF + XLSX, `rich+vlm` for PDF and `fast` for XLSX, returns both with their respective options applied (verifiable via `effective_options`).
- [ ] `image_export_mode: referenced` returns URLs that resolve from outside the docling-serve host (i.e., scratch is served behind the same ALB/auth).
- [ ] The existing `to_formats: md` only path returns a byte-identical markdown to today's deployment for a fixed test corpus (no regression).
- [ ] OTEL metrics for the new histograms are emitted.
- [ ] A consumer-facing changelog entry documents the new options and the response shape additions.

---

## 11. References

- Docling Serve repo: `/opt/captify-apps/docling-serve/` (this repo)
- Usage docs: `/opt/captify-apps/docling-serve/docs/usage.md`
- Configuration docs: `/opt/captify-apps/docling-serve/docs/configuration.md`
- Models handling: `/opt/captify-apps/docling-serve/docs/models.md`
- Existing Captify integration: `/opt/captify-apps/captify-core-wiki/lib/spaces/services/docling.service.ts`
- GPU runbook: `/opt/captify-apps/captify-core-wiki/docs/runbooks/docling-gpu-fix.md`
- Empirical test outputs (2026-05-19): `/tmp/docling-test/out/` — pdf.json, docx.json, xlsx.json, pptx.json
- Upstream Docling: <https://github.com/docling-project/docling>
- Upstream Docling Serve: <https://github.com/docling-project/docling-serve>
- SmolVLM (default picture description model): <https://huggingface.co/HuggingFaceTB/SmolVLM-Instruct>
- Granite Docling (default whole-document VLM): IBM Granite Docling family on HuggingFace
