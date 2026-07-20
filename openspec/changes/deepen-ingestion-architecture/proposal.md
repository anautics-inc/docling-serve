# Proposal: Deepen ingestion architecture

## Why

Graphify shows healthy package direction but incomplete runtime ownership. The
capability registry declares policy while routes repeat dispatch, readiness
repeats availability, typed extraction bypasses staged-upload policy, and Local,
RQ, and Ray repeat task lifecycle behavior. The only production import cycle is
between schematic extraction and revision.

## What Changes

- Make document capabilities executable adapters owning admission, readiness,
  extraction dispatch, profiles, OCR defaults, and output contracts.
- Apply one tenant-scoped upload and cleanup lifecycle to generic and typed work.
- Decompose FastAPI routes while retaining a thin `create_app()` composition root.
- Consolidate Local, RQ, and Ray staged-task execution and jobkit configuration.
- Break the schematic cycle and move generic publication out of that domain.
- Split staging and legacy internals behind stable compatibility facades.
- Add production-only Graphify architecture ratchets.

## Capabilities

### New Capabilities

- `executable-ingestion-adapters`
- `ingestion-execution-boundaries`
- `ingestion-architecture-ratchets`

### Modified Capabilities

- `document-ingestion-capabilities`
- `production-ingestion-policy`

## Impact

- `docling-serve`: capability, route, staging, worker, schematic, settings,
  tests, and CI boundaries.
- Existing HTTP and bundle contracts remain stable.
- Capability/readiness responses gain graph-extraction metadata additively.
- Pytology contracts are verification gates; no planned client behavior change.

## Non-goals

- Removing a supported format, engine, or legacy Office conversion.
- Renaming endpoints, request fields, or schema identifiers.
- Replacing Docling, S3, Redis, Ray, RQ, or model providers.
- Optimizing total graph node or edge count.

## Open Questions

None.
# Proposal: Deepen ingestion architecture

## Why

The hardened ingestion contracts are implemented, but Graphify shows that their
runtime ownership remains distributed. The capability registry declares policy
while `app.py` repeats dispatch, `adapter_readiness.py` repeats availability,
and typed routes bypass the staged-upload lifecycle. Local, RQ, and Ray workers
also repeat materialize/run/cleanup behavior. This creates correctness drift
despite stable public contracts.

The current graph contains 3,003 nodes and 6,838 edges across 92 production
files. `app.py` has 27 production import dependencies, `settings.py` has 13
production importers, and the only production import cycle is
`schematic_extractor.py` ↔ `schematic_revision.py`.

## What Changes

- Turn document capabilities into executable adapters that own admission,
  readiness, extraction dispatch, profiles, OCR defaults, and output contracts.
- Route explicit and automatic extraction through shared application services.
- Apply one tenant-scoped upload admission and cleanup lifecycle to generic and
  typed extraction.
- Keep `create_app()` as a thin composition root and move domain HTTP concerns
  into routers with injected services.
- Consolidate Local, RQ, and Ray staged-task execution and jobkit configuration.
- Break the schematic extraction/revision import cycle and move generic artifact
  publication out of the schematic domain.
- Split staging and legacy Office internals behind compatibility facades.
- Add Graphify architecture ratchets for cycles, fan-out, fan-in, and hotspot
  concentration.

## Capabilities

### New Capabilities

- `executable-ingestion-adapters`
- `ingestion-execution-boundaries`
- `ingestion-architecture-ratchets`

### Modified Capabilities

- `document-ingestion-capabilities`
- `production-ingestion-policy`

## Impact

- `docling-serve`: internal capability, route, staging, worker, schematic,
  settings, and architecture-test boundaries.
- Public HTTP endpoint paths and existing response contracts remain compatible.
- `/v1/capabilities` and `/ready/adapters` may add an explicit graph-extraction
  service capability; this is additive.
- No consumer changes are required unless contract tests reveal undocumented
  reliance on internal dispatch behavior.

## Non-goals

- Removing Local, RQ, Ray, legacy Office, or any supported document family.
- Renaming stable endpoints or bundle schema identifiers.
- Replacing Docling, Redis, S3, or model providers.
- Treating protocol constants as deployment configuration.
- Reducing total graph node or edge counts as an end in itself.

## Open Questions

None.
