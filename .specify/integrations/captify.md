# Captify ↔ Docling Serve Integration

**Audience:** docling-serve maintainers and the docling agent.
**Purpose:** describe everything the Captify platform sends to docling-serve, the response shapes Captify reads, and how to test the Captify endpoint against this docling-serve instance.
**Last reviewed:** 2026-05-19 (against docling-serve 1.16.1, captify-core-wiki main).

---

## 1. TL;DR

Captify uses **four** docling-serve endpoints, sends **at most three** parameters, and consumes a **small subset** of the response shape. Most of docling-serve's surface (enrichments, image modes, output formats beyond markdown, custom configs) is **untouched**.

| What | Where it lives in Captify | Sent to docling-serve |
|---|---|---|
| Health probe | `DoclingService.healthCheck()` | `GET /health` |
| Async chunk (RAG path) | `DoclingService.chunkFile()` | `POST /v1/chunk/{strategy}/file/async` + poll + result |
| Sync convert (preview path) | `DoclingService.convertFile()` | `POST /v1/convert/file` |
| Async convert (markdown path) | `DoclingService.convertDocument()` | `POST /v1/convert/file/async` + poll + result |

Only ~3 parameters ever leave Captify: `files` (binary), `to_formats=md` (convert only), and `do_ocr=true` + `ocr_engine=<name>` (when user picks non-auto OCR). Everything else uses docling-serve defaults.

---

## 2. Endpoints Captify hits

### 2.1 `GET /health`

- **Caller:** `DoclingService.healthCheck()` at `lib/spaces/services/docling.service.ts:44`
- **Used by:** the staging/prod liveness check — Captify probes this before declaring docling reachable in admin diagnostics
- **Timeout:** 5 s (`AbortSignal.timeout(5000)`)
- **Pass criteria:** any 2xx response. Body content is ignored.

**Behavior contract:** the endpoint must respond <5 s. A `{ "status": "ok" }` body is fine but not required.

### 2.2 `POST /v1/chunk/{strategy}/file/async`

This is the **primary path** — Captify uses chunks for embedding generation and OpenSearch indexing.

- **Caller:** `DoclingService.chunkFile()` at `lib/spaces/services/docling.service.ts:67`
- **Submit timeout:** 30 s
- **Total wall-clock:** 5 min (`DOCLING_REQUEST_TIMEOUT_MS = 300_000`)
- **Path strategies in use:** `hybrid` (default), `hierarchical` (when user opts in)
- **Form fields sent:**
  - `files`: binary blob, original filename preserved
  - `do_ocr`: `"true"` — only when user picks a non-AUTO engine
  - `ocr_engine`: one of `easyocr`, `tesseract_cli`, `tesseract` — only when user picks one

**Submit response Captify expects:**
```json
{ "task_id": "<uuid>" }
```

If `task_id` is missing → Captify raises `DoclingProcessingError("No task_id in Docling async chunk response")`.

**Polling loop** (`DoclingService.pollTask` at `:208`):
- Polls `GET /v1/status/poll/{task_id}?wait=1`
- Interval: **15 s** between polls
- Max iterations: 120 (i.e., 30 min hard ceiling)
- Tolerates 3 consecutive poll failures before bailing
- Expects `task_status` (case-insensitive) terminal values: `SUCCESS` | `FAILURE`
- On `FAILURE`, reads `task_meta.num_processed` and `task_meta.num_failed` to enrich the error message

**Result fetch:**
- `GET /v1/result/{task_id}` after terminal status
- Captify reads `chunks: []` from the top of the response
- Each chunk is normalized via `normalizeDoclingChunk` (see §3.2) to handle the historic top-level-vs-nested-`meta` variance.

### 2.3 `POST /v1/convert/file` (synchronous)

- **Caller:** `DoclingService.convertFile()` at `lib/spaces/services/docling.service.ts:177`
- **Timeout:** 5 min
- **Form fields sent:** `files` only
- **Used by:** rarely — preview path. Most flows go through the async convert below.

### 2.4 `POST /v1/convert/file/async` (asynchronous)

- **Caller:** `DoclingService.convertDocument()` at `lib/spaces/services/docling.service.ts:298`
- **Submit timeout:** 30 s
- **Total wall-clock:** 5 min (same `DOCLING_REQUEST_TIMEOUT_MS`)
- **Form fields sent:**
  - `files`: binary blob
  - `to_formats`: `"md"` — **always exactly `md`** (Captify discards the other 5 representations: `html_content`, `json_content`, `text_content`, `doctags_content`, `vtt`)
  - `do_ocr` + `ocr_engine`: same conditional as chunk path

Same `task_id` → poll → result pattern as §2.2. Captify reads `document.md_content` (string or null) and discards the rest of the response.

---

## 3. Response shapes Captify depends on

### 3.1 Async submit response

```json
{ "task_id": "<uuid>" }
```

`task_id` is the only field Captify reads.

### 3.2 Chunk result response (from `/v1/result/{task_id}` on the chunk path)

Captify expects:
```jsonc
{
  "chunks": [
    {
      // EITHER top-level OR nested under "meta" — Captify normalizes both
      "doc_items":    [...],          // strings OR DocItem objects with self_ref/label
      "page_numbers": [1, 2, ...],
      "headings":     ["..."],

      // Always top-level
      "filename":     "...",
      "chunk_index":  0,
      "text":         "...",
      "raw_text":     "...",          // optional
      "num_tokens":   123,             // optional
      "captions":     ["..."],         // optional
      "metadata":     {...}            // optional
    }
  ],
  // Optional, Captify logs presence but doesn't fail without them:
  "document": {...},
  "pages":    [...],
  "tables":   [...],
  "pictures": [...]
}
```

**Critical compatibility behavior** (`normalizeDoclingChunk` at `lib/spaces/spaces.utilities.ts`):
- Reads `doc_items` from **top-level first**, falls back to `meta.doc_items`
- Reads `page_numbers` from **top-level first**, falls back to `meta.page_numbers`
- For `doc_items` entries:
  - `string` → kept as-is
  - object with `self_ref` (e.g. `"#/tables/0"`) → uses `self_ref`
  - object with `label` (no `self_ref`) → uses `label`
  - anything else → `JSON.stringify(item)`

This shim was added because different docling-serve versions emitted these fields differently. If docling-serve standardizes one shape going forward, Captify will keep working — but please don't *remove* either shape without notice.

### 3.3 Convert result response (from `/v1/result/{task_id}` on the async convert path)

Captify reads exactly one field:
```jsonc
{
  "document": {
    "md_content": "..."   // string or null
  }
}
```

All other fields (`json_content`, `html_content`, `text_content`, `doctags_content`, `filename`, `status`, `processing_time`, `timings`, `errors`) are present in the response and ignored.

### 3.4 Poll status response

```jsonc
{
  "task_status": "PENDING" | "STARTED" | "SUCCESS" | "FAILURE" | ...,
  "task_meta": {
    "num_processed": 0,    // read only on FAILURE
    "num_failed":    0
  }
}
```

Captify uppercases `task_status` and matches against `["SUCCESS", "FAILURE"]`. Any other value is treated as "keep polling."

### 3.5 Sync convert response (`POST /v1/convert/file`)

Captify reads the entire response into a `DoclingConvertResponse` shape:
```typescript
interface DoclingConvertResponse {
  content:  string;
  metadata?: Record<string, unknown>;
}
```

This is the legacy preview path and is rarely exercised.

---

## 4. What Captify does *not* send

Awareness of the gap is useful for the docling agent when proposing additions:

| Docling option | Captify status | Notes |
|---|---|---|
| `to_formats` (anything beyond `md`) | ❌ never sent | Discards 5 representations per document |
| `image_export_mode` | ❌ defaults to `embedded` | Means inline base64 in markdown |
| `include_images`, `images_scale` | ❌ never set | Default behavior |
| `pdf_backend` | ❌ never set | Uses docling default (`docling_parse`) |
| `table_mode`, `do_table_structure`, `table_cell_matching` | ❌ never set | Uses defaults |
| `pipeline` | ❌ never set | Uses default pipeline |
| `page_range` | ❌ never set | Always full document |
| `document_timeout` | ❌ never set (Captify enforces its own) | |
| `abort_on_error` | ❌ never set | |
| `md_page_break_placeholder` | ❌ never set | |
| `do_code_enrichment` | ❌ off | |
| `do_formula_enrichment` | ❌ off | LaTeX would be valuable; not wired |
| `do_picture_classification` | ❌ off | |
| `do_chart_extraction` | ❌ off | |
| `do_picture_description` | ❌ off | The VLM caption feature — not wired |
| `vlm_pipeline_*` | ❌ off | |
| `picture_description_*` | ❌ off | |
| `layout_*`, `*_custom_config`, `*_preset` | ❌ off | |
| `force_ocr` | ❌ off | |
| `ocr_lang`, `ocr_preset`, `ocr_custom_config` | ❌ off | |

The future direction (see `2026-05-19-configurable-extraction/spec.md`) is named profiles that bundle these options, sent in a single `extraction` form field.

---

## 5. Configuration & environment

### 5.1 Endpoint URL

Captify resolves the docling-serve URL from this priority order:
1. `DoclingService` constructor argument `deps.apiUrl`
2. `process.env.DOCLING_API_URL`
3. Fallback: `http://localhost:5001`

**Important normalization** (per `lib/spaces/services/docling.service.ts:35`):
- Trailing slashes are stripped
- Whitespace (including `\r` from CRLF env files) is trimmed

This was added because `docker run --env-file` doesn't strip carriage returns from CRLF-terminated env files, which produced URLs like `https://host.us \r/v1/chunk/...` — those would hard-fail with confusing errors.

### 5.2 Captify-side env vars relevant here

Read from `.env.local`:
```env
# Server-side (used by lib/spaces/services/docling.service.ts via DOCLING_API_URL alias)
DOCLING_ENDPOINT=http://localhost:3060

# Client-side (used in browser-facing references only)
NEXT_PUBLIC_DOCLING_ENDPOINT=https://dev.captify.io/api/docs
```

The mismatched env var names (`DOCLING_ENDPOINT` vs `DOCLING_API_URL`) is a known Captify-side wart; the service falls back to `DOCLING_API_URL` because that's what the code checks. Captify will reconcile this in a future cleanup.

### 5.3 Captify-side timeouts and limits

| Constant | Value | Source |
|---|---|---|
| Convert/chunk total wall-clock | 300,000 ms (5 min) | `KB_PROCESSING_CONFIG.DOCLING_REQUEST_TIMEOUT_MS` |
| Submit timeout | 30,000 ms | inline in service |
| Health check timeout | 5,000 ms | inline in service |
| Poll interval | 15,000 ms | inline in service |
| Max poll iterations | 120 (i.e., 30 min worst-case) | inline in service |
| Max poll retries on failure | 3 | inline in service |
| Default max file size | 100 MB | `KB_PROCESSING_CONFIG.DEFAULT_MAX_FILE_SIZE_MB` |

If docling-serve introduces server-side limits via `DOCLING_SERVE_MAX_DOCUMENT_TIMEOUT` / `DOCLING_SERVE_MAX_NUM_PAGES` / `DOCLING_SERVE_MAX_FILE_SIZE`, Captify will not detect or surface them gracefully unless the server returns an actionable 4xx with a human-readable body — Captify echoes that body into the error message.

---

## 6. Error contract Captify expects

Captify defines two error classes; see `lib/spaces/spaces.errors.ts`:

| Captify error | Trigger from docling-serve |
|---|---|
| `DoclingConnectionError` | `fetch` throws a `TypeError` with `"fetch"` or `"network"` in the message — i.e., DNS, TCP, TLS, or ALB failure |
| `DoclingProcessingError` | (a) submit response not 2xx; (b) missing `task_id` in submit response; (c) `task_status` reaches `FAILURE`; (d) result fetch not 2xx; (e) total wall-clock or iteration ceiling hit; (f) any other unexpected error during the flow |

The Captify error message **includes the response body** when the server returns a non-2xx. For ergonomic debugging from logs, please ensure error bodies are **plain-text or JSON with a `detail` / `error` / `message` field** — Captify currently just dumps the body verbatim into the error message.

---

## 7. End-to-end Captify flow (for context)

When a user uploads a document via the Spaces or Wiki UI:

```
1. UI calls   POST /api/spaces/datasets/{id}/documents      (presigned URL)
2. UI uploads file to S3 directly
3. UI calls   POST /api/spaces/datasets/{id}/documents      (commits document record)
4. SpacesIndexingService runs asynchronously:
   a. Downloads file from S3
   b. Calls docling.chunkFile(buffer, filename, {strategy, ocrEngine})  ← endpoint §2.2
      AND docling.convertDocument(buffer, filename, {ocrEngine})        ← endpoint §2.4
      in parallel
   c. Saves converted markdown back to S3 (sidecar)
   d. Extracts entities/relations via docling-serve POST /v1/graph/extract
      (docling-graph + LiteLLM; replaced the old AWS Comprehend step)
   e. Generates embeddings via Bedrock
   f. Indexes chunks to OpenSearch (datasetId as index suffix)
   g. Marks document COMPLETE in DynamoDB
5. Wiki agent later queries OpenSearch via /api/spaces/datasets/{id}/search
```

Both docling calls happen in Step 4b. If either fails, the whole step fails and the document is marked `FAILED`. The 5-min Captify wall-clock applies to each docling call independently, so a worst-case document budget is ~10 min.

---

## 8. How to test the Captify endpoint against this docling-serve instance

These are the integration checks that prove docling-serve is compatible with Captify. The dev agent should run these whenever docling-serve ships a release that touches the API surface.

### 8.1 Baseline conformance (Captify happy path)

```bash
# 8.1.a — Health
curl -fsS --max-time 5 "$DOCLING_API_URL/health"
# Expect: 2xx, any body

# 8.1.b — Async convert, markdown only (Captify's primary convert call)
SUBMIT=$(curl -sS -X POST \
  -F "files=@sample.pdf" \
  -F "to_formats=md" \
  --max-time 30 \
  "$DOCLING_API_URL/v1/convert/file/async")
TASK_ID=$(jq -r .task_id <<< "$SUBMIT")
test -n "$TASK_ID" && test "$TASK_ID" != "null" \
  || { echo "FAIL: missing task_id"; exit 1; }

# 8.1.c — Poll
for i in $(seq 1 30); do
  STATUS=$(curl -sS --max-time 10 "$DOCLING_API_URL/v1/status/poll/$TASK_ID?wait=5" \
    | jq -r .task_status)
  case "${STATUS^^}" in
    SUCCESS) break ;;
    FAILURE) echo "FAIL: task_status=FAILURE"; exit 1 ;;
  esac
  sleep 2
done

# 8.1.d — Result must contain document.md_content as a non-null string
curl -sS --max-time 30 "$DOCLING_API_URL/v1/result/$TASK_ID" \
  | jq -e '.document.md_content | type == "string" and length > 0' >/dev/null \
  || { echo "FAIL: document.md_content missing or empty"; exit 1; }
```

### 8.2 Chunk path with `doc_items` shape check

Captify is sensitive to the `doc_items` / `page_numbers` shape. Run this and inspect manually:

```bash
SUBMIT=$(curl -sS -X POST \
  -F "files=@sample.pdf" \
  --max-time 30 \
  "$DOCLING_API_URL/v1/chunk/hybrid/file/async")
TASK_ID=$(jq -r .task_id <<< "$SUBMIT")

# ... poll loop omitted ...

curl -sS "$DOCLING_API_URL/v1/result/$TASK_ID" \
  | jq '.chunks[0] | {
      has_top_doc_items:    (has("doc_items")),
      has_meta_doc_items:   (.meta // {} | has("doc_items")),
      has_top_page_numbers: (has("page_numbers")),
      has_meta_page_numbers:(.meta // {} | has("page_numbers")),
      doc_items_first:      (.doc_items // .meta.doc_items // [])[0]
    }'
```

Captify accepts either top-level or nested-under-`meta`. If a release moves to a third location (e.g. `meta.doc_layout.items`), Captify will silently lose `doc_items` and emit a debug log but otherwise succeed — please file an issue if that happens.

### 8.3 OCR engine names

Captify's UI exposes `easyocr`, `tesseract_cli`, `tesseract`. Verify each is accepted (server returns 2xx on submit) by re-running 8.1.b with `-F "do_ocr=true" -F "ocr_engine=<engine>"` for each. The `auto` value is **Captify-only** — Captify strips the parameter entirely when the user picks auto.

### 8.4 Failure modes

Trigger each failure path and verify Captify-readable error bodies:

| Trigger | Expected response | Captify behavior |
|---|---|---|
| Submit a corrupt file | 4xx with plain-text or `{"detail": "..."}` body | Surfaces body verbatim in `DoclingProcessingError` |
| Submit valid file, kill task mid-run | `task_status=FAILURE` with `task_meta.num_processed`/`num_failed` | Error message: `"Document conversion failed (processed: N, failed: M)..."` |
| Hold up the result so total wall-clock exceeds 5 min | Captify times out, no further docling call | Error message: `"Docling task <id> timed out after 5 min wall-clock deadline"` |

### 8.5 Round-trip integration (against a live captify-core-wiki instance)

If a wiki dev server is running (`http://localhost:3001`):

```bash
# Verify the Captify side reaches docling-serve through its full stack
curl -fsS http://localhost:3001/api/health/docling 2>/dev/null \
  || echo "(Captify-side health endpoint not exposed yet; manual ingestion test required)"
```

For full E2E: upload a small PDF via the Spaces UI and watch the document transition through `pending → parsing → chunking → analyzing → embedding → indexing → complete` in the UI. The "parsing" and "chunking" steps are docling-bound; a failure surfaces with the docling error body.

---

## 9. Known divergences from upstream defaults

Items where Captify's behavior differs from or constrains what docling-serve naturally does:

1. **Captify always sends `to_formats=md`.** If docling-serve changes the default output, Captify is unaffected — but if it makes `md` a non-default and requires explicit opt-in, Captify still works because it always opts in.
2. **Captify never sets `image_export_mode`,** so it receives the default. As of 1.16.x that's `embedded` (base64-in-markdown). If the default flips, Captify's stored markdown would change shape — please call out a default flip in the changelog.
3. **Captify's 5-min wall-clock is per-call,** independent of server-side `DOCLING_SERVE_MAX_DOCUMENT_TIMEOUT`. If the server takes 10 min, Captify will time out and orphan a still-running task. Server-side cleanup via `DOCLING_SERVE_SINGLE_USE_RESULTS=true` + `DOCLING_SERVE_RESULT_REMOVAL_DELAY` handles cleanup correctly.
4. **Captify polls every 15 s on the chunk path** but docling-serve's `?wait=5` parameter inside the poll allows the server to long-poll up to 5 s before responding. Net effect: at most 1 round-trip every 15 s, but server work begins immediately after submit.

---

## 10. Files to read for source-of-truth

If anything here disagrees with the code, the **code wins**:

- `/opt/captify-apps/captify-core-wiki/lib/spaces/services/docling.service.ts` — the client
- `/opt/captify-apps/captify-core-wiki/lib/spaces/services/spaces-indexing.service.ts` — the pipeline that calls it
- `/opt/captify-apps/captify-core-wiki/lib/spaces/spaces.interfaces.ts` — `DoclingChunk`, `DoclingConvertResult` shapes
- `/opt/captify-apps/captify-core-wiki/lib/spaces/spaces.utilities.ts` — `normalizeDoclingChunk` compatibility shim
- `/opt/captify-apps/captify-core-wiki/lib/spaces/spaces.constants.ts` — `KB_OCR_ENGINE`, `KB_CHUNKING_STRATEGY`, `KB_PROCESSING_CONFIG`
- `/opt/captify-apps/captify-core-wiki/lib/spaces/spaces.errors.ts` — `DoclingConnectionError`, `DoclingProcessingError`
- `/opt/captify-apps/captify-core-wiki/docs/runbooks/docling-gpu-fix.md` — operational runbook for the GPU bring-up

---

## 11. Companion specs

- `2026-05-19-configurable-extraction/spec.md` — the requirements for the next-generation extraction API. Once shipped, Captify will adopt named profiles and stop hand-rolling form fields.
