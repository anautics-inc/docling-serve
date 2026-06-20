# ADR 0001 — docling is the architecture: configure it, extend only via plugins

- **Status:** Accepted
- **Date:** 2026-06-17
- **Deciders:** Captify platform (anautics-inc/docling-serve)

## Context

Our goal is to extract as much structured information as possible from many
document types — Word, PowerPoint, Excel, plain text, PDF, Access databases —
and to be able to add new domain extractors (e.g. engineering schematics) over
time, at **distributed scale** and inside a **100% air-gapped (IL5)** runtime.

An earlier fork answered this with a large hand-rolled subsystem in
`docling-serve` (`deep_document/`, a custom extractor registry, custom S3
publish) wired into the job workers by **monkey-patching** docling-jobkit's
internal `process_export_results` / `process_chunk_results`. Investigation showed
this is fundamentally upgrade-fragile: docling-jobkit renamed/reshaped those
functions between 1.18 and 1.23 (`process_exportable_results`, new
`ExportableDocument` + `s3_presigned_config` signature), which would silently or
loudly break the patch. There is no first-class "result processor" hook in
jobkit at any version.

Crucially, most of that subsystem **re-implements what docling already does
natively**: docling parses PDF/DOCX/XLSX/PPTX/HTML/CSV/Markdown/TXT/images into a
rich `DoclingDocument`, with maintained enrichments (layout, reading order,
tables, code, formulas, image classification, picture description/VLM).

## Decision

**Treat docling as the architecture. Configure it minimally; do not rebuild it.
Extend only for genuine gaps, and only through docling's documented extension
seams.**

1. **Base on the latest upstream `docling-serve` (1.24.0)**, which already
   integrates `docling-jobkit>=1.23.1`, `docling-slim[...]>=2.101.2`,
   `docling-core>=2.79.0`. Track upstream; keep the captify delta as small as
   possible. Do **not** carry forward the 1.18-era fork subsystem.

2. **Standard formats use native docling backends** (Word/PPT/XLS/TXT/HTML/CSV/
   PDF/images). No custom adapters.

3. **"Extract as much as possible" = enable native enrichments** (tables,
   formulas, code, picture classification, picture description/VLM) via pipeline
   options — configuration, not code.

4. **Output to S3 uses jobkit's native pre-signed S3 target** (`S3PresignedConfig`
   / `S3PresignedTargetProcessor`, added jobkit 1.21, hardened 1.23.1): artifacts
   uploaded under task-scoped keys with `tenant_id`/`user_id`/`project_id`
   metadata, returned as pre-signed URLs. This replaces the fork's custom
   `s3_publisher`. If the notebook still requires an `extraction.json` manifest
   shape, that is a thin **custom serializer**, not a subsystem.

5. **Distributed execution is jobkit's job** — local/RQ/Ray orchestrators run the
   native `DocumentConverter`. We do not touch worker internals.

6. **Extend only for genuine gaps, as docling plugins** (setuptools
   `entry-points."docling"` + `allow_external_plugins`), which are auto-discovered
   in every worker (so they work distributed) and sit on docling's public,
   semver-managed contracts (so they survive upgrades):
   - **Access databases (.mdb/.accdb)** → a custom `DeclarativeDocumentBackend`
     plugin (new `InputFormat`).
   - **Schematics, and future domain extractors** → a custom `BasePipeline` /
     enrichment-model plugin, selected for PDF/image inputs by profile.
   - **Legacy binary Office (.doc/.xls/.ppt)** → thin LibreOffice pre-convert (or
     a small backend); docling has no native legacy-binary backend.

7. **Air-gapped (IL5):** bake all docling models **and** the chunker tokenizer at
   image build time; set `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE`/
   `HF_DATASETS_OFFLINE` so the runtime never reaches HuggingFace; pull Python
   wheels from the internal mirror via the pinned `uv.lock`. No new model
   downloads are introduced (graph/image enrichers use the LiteLLM→Bedrock proxy,
   not HF).

## Consequences

- **Retire** the fork's `deep_document/`, `extraction/`, custom extractor
  registry, `worker_hook.py`, and the orchestrator monkey-patch. Their genuine
  value (Access parsing, schematic logic, the bundle/manifest shape) is
  re-expressed as the plugins/serializer above.
- **Upgrade-safe:** we depend on docling's public backend/pipeline/plugin
  contracts and jobkit's public S3 target — not internal worker functions. A
  jobkit/docling bump can no longer silently disable our capability.
- **Minimal surface, maximal capability:** most extraction is native and improves
  with every docling release; our code is only the few true gaps.
- **Distributed + testable:** plugins are installed into worker images and
  auto-loaded by every orchestrator; each is independently unit-testable.

## Validation

Prove against `tests/test_files/` (real fixtures): native conversion +
enrichments for `.docx`, `.pptx`, `.xlsx`, `.pdf`, `.txt`; the Access backend
plugin against a `.mdb`; and the schematic pipeline plugin against
`main_schematic.pdf`. Confirm the whole flow runs with the HF stack forced
offline.

## FIPS self-test bypass (auditable hack)

The runner hosts that build our image are FIPS-enabled (`/proc/sys/crypto/
fips_enabled` reads `1`). PyTorch ships its own OpenSSL 1.x, which runs a FIPS
power-on self-test at import; that bundled OpenSSL is not a validated FIPS
module, so the self-test **aborts the process** the moment `torch` is imported
on a FIPS host. The model-download step (`docling-tools models download`) imports
torch, so the build cannot complete without neutralizing that check.

**The mechanism (build-time only):**

- `.gitlab-ci.yml` `.buildx_fips_bypass` creates a `docker buildx` builder
  (docker-container driver) whose `buildkitd.toml` enables
  `insecure-entitlements = ["security.insecure"]`. `build-image` /
  `build-sbom-image` run `docker buildx build --allow security.insecure`.
- In `Containerfile`, the model-download layer runs as `USER 0` under
  `RUN --security=insecure` (which grants `CAP_SYS_ADMIN`), writes `0` to
  `/tmp/fips_zero`, and `mount --bind`s that file over
  `/proc/sys/crypto/fips_enabled`. torch then reads FIPS as disabled and skips
  the self-test, the models + tokenizer bake, and the bind-mount disappears with
  the build container.

**Scope & residual risk:**

- **Build-time only.** The override exists only inside the model-download
  `RUN` layer during image build. Nothing in the bind-mount, the
  `security.insecure` entitlement, or the `USER 0` step persists into the
  runtime image (which runs `USER 1001`). The shipped container never alters
  `/proc/sys/crypto/fips_enabled`.
- **Residual risk:** `security.insecure` grants the build step elevated
  capabilities (`CAP_SYS_ADMIN`) on the builder host, so the builder must be a
  trusted runner. The override masks the *host* FIPS flag for that one layer; it
  does not make PyTorch's bundled OpenSSL FIPS-validated. Runtime cryptography is
  governed by the runtime base image / system OpenSSL, not this build hack. The
  bypass is enumerated here and in the constitution (Article V.4) so it is
  auditable rather than incidental.

## Notes on upstream contribution

If distributed post-processing beyond what plugins/serializers cover is ever
needed, the correct move is to contribute a first-class `result_processor` hook
to docling-jobkit upstream — never to re-introduce a worker monkey-patch.
