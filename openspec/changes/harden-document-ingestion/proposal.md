# Proposal: Harden document ingestion

## Why

Docling Serve supports generic documents, legacy Office files, Access databases,
XFA forms, technical orders, and engineering schematics, but capability policy
is distributed across routes, settings, workers, and downstream clients.
Production defaults also permit ambiguous tenant scope, wildcard CORS, and
implicit model-driven work. Dependency and container inputs are not uniformly
reproducible.

## What Changes

- Establish one typed registry for format admission, OCR, routing, runtime
  capabilities, and output contracts.
- Keep explicit typed routes while adding service-owned automatic extraction.
- Require tenant scope and safe authentication/CORS/model settings in production.
- Isolate legacy Office conversion behind a bounded adapter and granular readiness.
- Characterize every supported document family and enforce cross-repository contracts.
- Upgrade verified-compatible dependencies and pin build inputs.
- Remove only usage-proven dead compatibility code and stale operational guidance.

## Capabilities

### New Capabilities

- `document-ingestion-capabilities`
- `production-ingestion-policy`
- `dependency-provenance`

### Modified Capabilities

- Captify Pytology `canonical-document-ingest`
- Captify Core Spaces Docling client contract

## Impact

- `docling-serve`: API policy, capability routing, legacy adapter, tests, dependencies,
  container, and deployment documentation.
- `captify-pytology`: Docling client, worker routing, configuration, and contract tests.
- `captify-core`: Spaces Docling client configuration and contract tests.

## Non-goals

- Replacing Docling's conversion models.
- Dropping `.doc`, `.ppt`, or `.xls` support.
- Changing canonical document identity or downstream search/graph semantics.
- Renaming stable bundle schema identifiers without a versioned migration.

## Open Questions

None.
