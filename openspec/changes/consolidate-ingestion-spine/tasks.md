# Tasks: Consolidate ingestion spine

## 1. Specification

- [x] 1.1 Add proposal, design, requirement deltas, and traced tasks.
- [x] 1.2 Strictly validate OpenSpec before runtime changes.

## 2. Safe reductions

- [x] 2.1 Remove dead ingestion services and the unused adapter protocol.
- [x] 2.2 Remove test-only datamodel shims and readiness indirection.
- [x] 2.3 Remove unused package exports and orchestrator aliases.

## 3. Shared implementations

- [x] 3.1 Consolidate typed extraction and bundle publication.
- [x] 3.2 Consolidate common policy validation.
- [x] 3.3 Consolidate staged execution and KiCad export helpers.

## 4. Canonical task

- [x] 4.1 Add a format-neutral canonical task result.
- [x] 4.2 Move Access preparation and typed adapter dispatch into Docling Serve.
- [x] 4.3 Migrate Pytology to consume the canonical task result.
- [x] 4.4 Remove the in-process Docling library backend.
- [x] 4.5 Enforce required entity, index, and projection checkpoints.

## 5. Verification matrix

- [x] 5.1 Add a synthetic corpus covering every admitted extension and domain.
- [x] 5.2 Add environment-gated live Docling/OpenSearch/Neo4j matrix coverage.
- [x] 5.3 Remove test collection-order dependencies.

## 6. Ratchet and verify

- [x] 6.1 Run Docling and Pytology quality and contract suites.
- [x] 6.2 Run strict OpenSpec and both Graphify gates.
- [x] 6.3 Run container, legacy Office, ngspice, and SBOM smokes.
- [x] 6.4 Ratchet reviewed ceilings after all behavior gates pass.
