# Proposal: Consolidate the ingestion spine

## Why

Every canonical document reaches the shared search and graph pipeline after
chunking, but conversion still has bypasses. Access is rendered in Pytology,
the optional library backend bypasses Docling Serve, and typed documents make
a second extraction request. Graphify also identifies dead service layers,
test-only shims, and repeated extraction, policy, and execution code.

## What Changes

- Give one Docling task responsibility for admission, format routing,
  conversion, chunking, and optional typed artifacts.
- Make Pytology consume one canonical task result for every supported format.
- Remove the in-process Docling backend and Pytology-owned Access conversion.
- Require configured entity extraction, OpenSearch indexing, and Neo4j
  projection checkpoints before canonical completion.
- Remove dead and migration-complete compatibility layers.
- Consolidate repeated extraction, policy, execution, and rendering behavior.
- Add a golden format corpus and a live cross-service verification gate.

## Capabilities

### New Capabilities

- `canonical-ingestion-spine`
- `format-matrix-verification`
- `ingestion-code-reduction`

### Modified Capabilities

- `executable-ingestion-adapters`
- `ingestion-execution-boundaries`
- `ingestion-architecture-ratchets`

## Compatibility

- Preserve supported file families, HTTP paths, response schemas, bundle
  schemas, tenant/auth policy, and Local/RQ/Ray deployment modes.
- Existing typed routes remain as compatibility transports over shared domain
  services.
- Existing flat environment names remain authoritative.

## Non-goals

- Removing legacy Office, Access, form, technical-order, schematic, or graph
  extraction capabilities.
- Making model- or infrastructure-dependent tests part of the hermetic suite.
- Retaining unused private or test-only import paths.
