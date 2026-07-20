# Spec Delta: Canonical ingestion spine

## ADDED Requirements

### Requirement: Every admitted file uses one Docling task contract

Every supported file family SHALL be admitted, routed, converted, and chunked
through one asynchronous Docling task contract.

#### Scenario: Access database is submitted

- **WHEN** Pytology submits an MDB or ACCDB source
- **THEN** Docling Serve owns Access rendering and returns canonical markdown
  and chunks without Pytology preconverting the source

#### Scenario: A typed document is submitted

- **WHEN** a form, technical order, or schematic is selected
- **THEN** its routing and typed artifacts are produced by the same task that
  returns canonical markdown and chunks

### Requirement: Canonical task results are format neutral

Task results SHALL expose normalized markdown, chunks, routing metadata, and
optional typed artifact metadata without requiring format-specific client
dispatch.

#### Scenario: Pytology consumes different formats

- **WHEN** PDF, Office, tabular, Access, image, or typed inputs complete
- **THEN** the worker uses the same result normalization and downstream call

### Requirement: Production extraction has one backend

Canonical production ingestion SHALL use Docling Serve and SHALL NOT fall back
to an in-process Docling library backend.

#### Scenario: Docling Serve is unavailable

- **WHEN** canonical ingest is requested
- **THEN** submission fails closed instead of selecting a different converter

### Requirement: Completion proves semantic and storage checkpoints

When entity extraction is enabled and configured as required, canonical
completion SHALL require entity-stage success, OpenSearch index evidence, and
Neo4j document/chunk projection evidence.

#### Scenario: Entity extraction provider fails

- **WHEN** required graph extraction fails
- **THEN** the job does not complete with an empty semantic result
