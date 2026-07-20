# Document ingestion deployment

## Required production posture

- Set `DOCLING_SERVE_DEPLOYMENT_MODE=production`.
- Use machine assertions or a deployment-scoped API key. `auth_mode=none` is
  rejected in production.
- Send the configured tenant header on every document, task-status, and
  task-result request. No production request receives a shared default tenant.
- Use an explicit CORS allowlist, or leave CORS origins empty for
  service-to-service deployments.
- Keep remote model features disabled unless their LiteLLM transport, model
  alias, timeout, retry, page/call/token budget, and usage telemetry are set.

## Capacity model

The local orchestrator is for one worker process and bounded local workloads.
Do not scale it by starting multiple API workers against shared local state.
Use RQ for ordinary multi-worker deployments and Ray only where its fair
tenant scheduler and accelerator topology are required. Durable upload staging
is required whenever submission and processing can occur in different
processes or pods.

## Capability readiness

`GET /ready` reports mandatory service dependencies. `GET /ready/adapters`
reports optional format adapters independently, and `GET /v1/capabilities`
publishes extensions, media types, OCR defaults, output contracts, and current
availability. A missing LibreOffice runtime disables the isolated
`legacy-office` fallback without taking generic PDF/DOCX ingestion down.

Current Docling releases also admit `.doc`, `.ppt`, and `.xls` natively. The
isolated LibreOffice adapter remains a bounded fallback for historical files
that need normalization. Install it during image construction; service startup
never installs packages or mutates its host.

## Offline operation

Production images set Hugging Face, Transformers, and Datasets offline modes.
All enabled conversion, OCR, tokenizer, and enrichment artifacts must be baked
and validated before deployment. Readiness must report a missing optional
artifact; runtime download or model egress is not a recovery mechanism.

## Artifact policy

Generic conversion uses native versioned Docling artifacts. Form, BOM, and
schematic extractors publish the schema named by `/v1/capabilities`. Storage
prefixes are tenant/task scoped, source objects are immutable and scanned, and
schema changes require dual-read migration before a writer version changes.
