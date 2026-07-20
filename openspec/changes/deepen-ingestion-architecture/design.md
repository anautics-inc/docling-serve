# Design: Deepen ingestion architecture

## Context

The hardened public contracts are stable, but runtime policy is spread across
`capabilities.py`, `adapter_readiness.py`, route closures, worker wrappers, and
domain modules. Graphify reports 27 production dependencies from `app.py`, 13
production importers of settings, and one two-file schematic cycle.

## Approaches considered

### A. Extract files while preserving distributed policy

Move route and helper code into smaller modules without changing ownership.
This reduces line counts but leaves admission, readiness, dispatch, and cleanup
duplicated. Drift remains possible and graph improvements are mostly cosmetic.

### B. Introduce application services and executable adapters first

Create adapters that own capability behavior, a shared admission service, a
shared artifact publisher, and an engine-neutral task envelope. HTTP and queue
modules then become transport adapters over those services. Physical file
splits happen behind compatibility facades after behavior is centralized.

Selected: **B**. It changes ownership before layout and gives every later move a
stable test seam.

## Target architecture

1. HTTP routers translate protocol inputs into an admitted source.
2. Admission applies tenant, byte-limit, staging, and cleanup policy.
3. The executable registry selects one adapter for explicit or automatic work.
4. Adapters call domain extractors and a neutral artifact publisher.
5. Local, RQ, and Ray invoke one staged-task execution envelope.
6. Settings are snapshotted at boot and narrowed into immutable adapter views.

## Compatibility

- Stable route paths, forms, responses, task entrypoints, and bundle contracts
  do not change.
- Moved public symbols remain available through temporary re-export facades.
- Existing flat `DOCLING_SERVE_*` environment names remain authoritative.
- Graph extraction is additive capability metadata and remains disabled by default.

## Testing seams

- Pure registry resolution, profile aliases, OCR policy, and bounded PDF probes.
- Admission with fake staging/materialization and deterministic cleanup.
- Adapter services independent of FastAPI.
- Router composition and OpenAPI route parity.
- Parametrized Local/RQ/Ray staged execution.
- Config-builder parity between service and CLI worker boot.
- Schematic regeneration and delivery checks without cyclic imports.
- Public facade exports and environment alias parity.
- Production-only Graphify metrics.

## Rollout and ordering

1. Record OpenSpec and architecture baseline.
2. Centralize adapters, signals, and readiness.
3. Add admission and artifact services.
4. Extract routers.
5. Consolidate jobkit and worker execution.
6. Break schematic cycle and split hotspots.
7. Tighten architecture ceilings after behavioral gates pass.

## Decision log

- Keep `create_app()` as the composition root, not a domain implementation.
- Keep the RQ string entrypoint and Ray deployment import paths stable.
- Use compatibility facades rather than a flag-day import migration.
- Ratchet production coupling, not Graphify totals or external dangling edges.
- Keep external-tool imports lazy so optional adapters do not break startup.
