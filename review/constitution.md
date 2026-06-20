# docling-serve Constitution

**Scope:** the Captify fork of `docling-serve` (`anautics-inc/docling-serve`). This
document is the source of truth for *how we configure docling, manage documents,
index/chunk, and run extraction* on top of upstream. It governs every change to
this repository. The companion gate `review/final-review.md` MUST pass before any
commit.

**Version:** 1.0.0 · **Ratified:** 2026-06-20 · Supersedes ad-hoc fork practice.

---

## Article 0 — Precedence & non-negotiables

These rules take precedence over personal preference and prior fork conventions.
If a task cannot be done without violating one, **stop and surface the conflict**.

- **N1. docling is the architecture (ADR-0001).** Configure docling; do not rebuild
  what it already does. Extend ONLY through docling's documented seams
  (`entry-points."docling"` plugins, `BasePipeline`/`DeclarativeDocumentBackend`,
  custom serializers). **No monkey-patching** of docling/docling-jobkit internals
  (`process_export_results`, worker hooks, orchestrator internals) — ever.
- **N2. Track upstream; keep the delta minimal.** Base on the latest upstream
  release (currently 1.24.0 line). Every Captify-added file/setting must justify
  its existence against "could docling/jobkit do this natively?".
- **N3. Air-gapped (IL5) by construction.** The runtime must never reach
  HuggingFace or the public internet for models. All weights + tokenizers are baked
  at image-build time and `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE`/
  `HF_DATASETS_OFFLINE` are pinned.
- **N4. No secrets in code, logs, or URLs.** API keys are `SecretStr`, sourced from
  env/secret store, never logged, never passed as query parameters.
- **N5. Secure-by-default.** A misconfiguration must fail closed, not open. Auth,
  tenant handling, and template selection default to the safe choice.

---

## Article I — Configuration over code (docling pipeline)

docling parses PDF/DOCX/XLSX/PPTX/HTML/CSV/MD/TXT/images into a rich
`DoclingDocument` with maintained enrichments. "Extract as much as possible" means
**enable native enrichments via pipeline options**, not hand-rolled parsers.

1. **Pipeline options are configuration, not branching code.** Express conversion
   behavior through `PdfFormatOption(pipeline_options=PdfPipelineOptions(...))` and
   the service's `ServicePolicy`/`convert_options` normalization — not bespoke
   per-format `if` ladders.
2. **Tables:** default to `do_table_structure=True` with
   `TableFormerMode.ACCURATE` for correctness-sensitive corpora; allow `FAST` only
   as an explicit, documented performance preset. Surface `do_cell_matching` as a
   tunable (mis-merged columns ⇒ `do_cell_matching=False`).
3. **OCR:** `do_ocr` is opt-in per request/preset (it multiplies latency). The OCR
   engine must be installed in the image before it is offered as a preset. Prefer
   engines that tolerate read-only filesystems in distributed workers.
4. **Enrichments** (`do_formula_enrichment`, `do_code_enrichment`,
   `do_picture_classification`, `do_picture_description`) are enabled via options.
   Picture-description / VLM enrichers MUST route to the LiteLLM→Bedrock proxy, not
   pull a new HF model at runtime (see N3).
5. **Performance guards are mandatory in production paths:** a `document_timeout`
   (90–120s baseline), `max_num_pages`/`max_file_size` limits, and image-scale /
   page-image generation tuned for memory. These belong in settings/policy with
   safe defaults — never unbounded.
6. **`allow_external_plugins` is OFF unless a vetted plugin requires it**, and then
   only for the specific, reviewed plugin (ADR-0001 §6). It is a code-execution
   surface.

> Litmus test: if a change adds a parser/adapter for a format docling already
> supports, it is wrong. Re-express it as pipeline options or a plugin.

---

## Article II — Document management

1. **Standard formats use native docling backends.** No custom adapters for
   Word/PPT/XLS/TXT/HTML/CSV/PDF/images. Genuine gaps (Access `.mdb/.accdb`, legacy
   binary Office, schematics) are docling **plugins**, not subsystems.
2. **Output to S3 uses jobkit's native pre-signed target** (`S3PresignedConfig` /
   `S3PresignedTargetProcessor`) with task-scoped keys carrying
   `tenant_id`/`user_id`/`project_id` metadata. No custom S3 publisher. A required
   manifest shape (e.g. `extraction.json`) is a thin **serializer**, not a pipeline.
3. **Models live on a mounted artifacts path** (`DOCLING_SERVE_ARTIFACTS_PATH`)
   pre-baked at build (PVC/Job/volume), never auto-downloaded at runtime.
4. **Inputs are validated and bounded** before work is enqueued: format allow-list,
   size, page count, and (for URL sources) SSRF-safe fetching. Reject early with a
   422, do not fail deep in a worker.
5. **No real customer/CUI corpora committed to the repo.** Test fixtures are small,
   synthetic, and license-clean. Large or sensitive documents belong in object
   storage referenced by the test, not in git history.

---

## Article III — Indexing & chunking

When this service produces chunks for downstream RAG/indexing:

1. **Use docling's `HybridChunker`** (hierarchical + token-aware) — do not
   re-implement fixed-size/character splitting.
2. **Tokenizer alignment is mandatory.** The chunker tokenizer MUST match the
   downstream embedding model's tokenizer. The tokenizer is baked offline at build
   time (N3); a tokenizer mismatch is an indexing-correctness bug, not a warning.
3. **Embed the `contextualize()` output**, not raw `chunk.text` — chunks carry
   heading/caption/table context that materially improves retrieval.
4. **Preserve structure across splits:** keep `repeat_table_header=True` and
   `merge_peers=True` defaults unless a measured reason says otherwise. Custom
   serialization (tables→Markdown, picture annotations) goes through a
   `SerializerProvider`, not post-hoc string surgery.
5. **Chunk IDs are stable and deterministic** for the same input, so re-indexing is
   idempotent.

---

## Article IV — Extraction algorithms (knowledge graph / NER)

The graph module (`docling_serve/graph/*`) replaces AWS Comprehend NER with
template-driven, schema-validated extraction via `docling-graph` over LiteLLM.

1. **Template-first.** Extraction quality is bounded by the Pydantic template. Ship
   a generic `DocumentGraph` fallback plus specific domain templates; select by
   `profile`. Prefer the most specific template for a known document type.
2. **Templates are an allow-list, never arbitrary import.** A request's `template`
   field MUST be validated against `_allowed_templates()` before any
   `importlib.import_module` — an unrestricted dotted path is a code-execution
   gadget. New templates are added to the allow-list/profile map in code review.
3. **Controlled vocabulary & consistency.** Entity types and relation predicates
   follow the platform's controlled vocabulary (PascalCase types,
   SCREAMING_SNAKE_CASE predicates); the same entity gets the same type every time.
   Domain vocabularies (e.g. USAF sustainment) are normative and mirror the
   ingestion default vocabulary so results publish instead of queuing as proposed.
4. **Stable output contract.** The `{nodes, edges, labels, edgeLabels, ...}` shape
   is consumed by `captify_enterprise.search.graph_entities`. Do not change it
   without updating that consumer; treat it as a versioned API.
5. **Graceful degradation, not hard failure.** When `docling-graph` is unavailable
   or LiteLLM is unconfigured, return an **empty graph with a `note`** — callers
   handle "no graph" uniformly. Internal errors surface `type(err).__name__` only,
   never raw proxy/LLM error text.
6. **Bounded resources.** Truncate input to `max_chars`; pin real model token
   budgets (`max_output_tokens`, `context_limit`) so document-scale extractions are
   not silently truncated mid-JSON. Temp files are always cleaned up in `finally`.
7. **Spend attribution, not isolation.** Identity/tenant headers forwarded to the
   proxy are for spend tagging only and MUST be documented as such — they are not
   an authorization boundary (see Article VI).

---

## Article V — Air-gapped / IL5 runtime

1. **Bake everything at build time:** all docling models in `MODELS_LIST` plus the
   chunker tokenizer (`AutoTokenizer.from_pretrained(...)`). A runtime model
   download is a release blocker.
2. **Pin offline env** (`HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`,
   `HF_DATASETS_OFFLINE=1`) after install; never unset in entrypoints.
3. **Wheels from the internal mirror via the frozen `uv.lock`.** `uv sync
   --frozen`; no unpinned/dynamic installs.
4. **Document intentional hacks.** The FIPS self-test bypass mount and any
   privileged build step must carry an inline rationale and be enumerated here and
   in the ADR — they are auditable, not incidental.
5. **No new model dependency may be introduced** without a build-time bake plan.
   Graph/image enrichers use the proxy, not HF.

---

## Article VI — Security & multi-tenancy

1. **Auth is IP-aware and fails closed for public callers.** Endpoints require
   `X-Api-Key` via the shared `require_auth` dependency for any non-local caller.
   Loopback/private (RFC1918/link-local/ULA) clients are exempt by default
   (`auth_allow_private_networks`) so a local install runs with no key, while a
   public caller is rejected when no key is configured — the service can never be
   silently exposed unauthenticated. The private-network check trusts only the
   socket peer, never `X-Forwarded-For`; behind a private-network proxy, set
   `auth_allow_private_networks=false` + a key. `allow_unauthenticated=true` (full
   bypass) is dev/test only. Startup refuses to run only in the unusable
   fully-locked config (no key, no private bypass, not unauthenticated).
2. **Every new endpoint is authenticated by default** and uses the header-based
   dependency. **No secrets in query parameters** — the websocket key-as-query-param
   pattern is a known defect to be migrated to a header/subprotocol, not copied.
3. **Secrets are `SecretStr`** end-to-end (`litellm_api_key`,
   `graph_litellm_api_key`, `api_key`), never plain `str` on an `extra="allow"`
   settings model, never in `repr()`/`model_dump()` output.
4. **Tenant headers are untrusted input.** `tenant_id` is caller-supplied and used
   only for fairness/metrics/spend. Any feature that treats it as an isolation
   boundary MUST validate it against the authenticated principal first.
5. **SSRF / path safety:** URL sources and S3 targets are validated against
   allow-lists; template/import paths are allow-listed (Article IV).

---

## Article VII — Observability & error handling

1. **No identity/PII at INFO.** Tenant IDs, filenames, sizes, and md5s of uploads
   are DEBUG at most. The pervasive `[TENANT_ID] ... header_value='...'` INFO
   logging is a compliance defect to remove/downgrade. Never log secrets.
2. **Structured, leveled logging via `logging`** — no `print()` in any request
   path. `print`-based tools (e.g. `debug_ray_state.py`) are CLI-only and must
   never be imported by serving code.
3. **Errors are typed and graceful.** Use the module's custom exception
   (`GraphExtractionUnavailable`) and map to the documented degraded response.
   Never leak upstream/proxy internals to the caller.
4. **Configuration failures are loud.** Do NOT silently swallow exceptions when
   loading config/secrets (`except Exception: return {}`) — log the failure;
   malformed config must be visible.
5. **Health/readiness honesty.** `/health`/`/ready` reflect real model-load state
   (503 until ready); OTEL spans/metrics carry tenant only where already present.

---

## Article VIII — Code quality, settings & tests

1. **Settings are statically typed and documented.** Every Captify setting lives on
   the settings model with a real type and a comment; defensive
   `getattr(settings, "x", default)` for fields that are statically defined is
   forbidden (it masks typos). Operator-facing settings are documented in
   `docs/configuration.md` and `.env.example`.
2. **Async hygiene.** Blocking work (LLM/proxy calls, conversion) runs off the event
   loop — either a sync `def` endpoint (threadpool) or an awaited executor. Never
   block the event loop in an `async def`.
3. **Tests exist for every Captify-added surface.** At minimum: graph template
   allow-list rejection, `graph_payload_from_text` happy/empty/error paths,
   `_graph_to_payload` shape, the `/v1/graph/extract` endpoint (auth + degraded
   note), policy validation, and auth open/closed behavior. New behavior ships with
   tests in the same change.
4. **Lint/format/type gates are real.** Ruff (including no-`print` in serving code),
   formatting, and typing pass. Re-enabling upstream-disabled bans
   (`T20`, bugbear `B`, mypy strict) for Captify modules is encouraged, never
   loosened further.
5. **No dead artifacts.** No stale `.pyc` / retired-fork leftovers
   (`deep_document/`, `worker_hook.py`, `s3_publisher`, custom extractor registry)
   reappear. The ADR-0001 retirement is permanent.

---

## Article IX — Upgrade safety & change discipline

1. **Depend on public contracts only** (docling backend/pipeline/plugin APIs,
   jobkit's public S3 target). A jobkit/docling bump must not be able to silently
   disable a Captify capability.
2. **Record decisions as ADRs** under `docs/adr/`. A change that alters the
   extension strategy, the graph output contract, or the offline posture needs an
   ADR.
3. **Conventional commits**, scoped to `docling_serve/graph/*`, settings, the graph
   endpoint, `policy.py`, the `Containerfile` air-gap block, CI, and docs. Changes
   outside that surface need explicit justification (N2).
4. **The gate runs before commit.** `review/final-review.md` must report PASS (or an
   explicitly waived, documented blocker) before `git commit`.

---

## Litmus summary

- Adding a parser for a format docling supports? → **No.** Configure or plugin.
- Monkey-patching jobkit/worker internals? → **No.** Never.
- New runtime model download? → **No.** Bake at build.
- Secret in a log line or URL? → **No.** `SecretStr`, header only.
- Arbitrary `template` import from a request? → **No.** Allow-list.
- Empty graph + `note` on failure? → **Yes.** Degrade uniformly.
- Tokenizer aligned to the embedding model, baked offline? → **Yes.**
- New Captify surface without a test? → **No.** Ship the test.
