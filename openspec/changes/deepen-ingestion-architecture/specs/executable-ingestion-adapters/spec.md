# Spec Delta: Executable ingestion adapters

## ADDED Requirements

### Requirement: Capability declarations are executable

Each declared ingestion capability SHALL resolve to one adapter owning its
admission policy, accepted profiles, OCR default, readiness probe, extraction
behavior, and output contract.

#### Scenario: A capability is added

- **WHEN** a new document family is registered
- **THEN** explicit extraction, automatic dispatch, readiness, and public
  capability metadata resolve from that registration without route-local maps

#### Scenario: An optional adapter is unavailable

- **WHEN** its runtime probe fails
- **THEN** only that adapter reports unavailable and unrelated adapters remain ready

### Requirement: Routing signals and profile aliases have one owner

Bounded source probes and profile aliases SHALL be defined once and reused by
automatic and explicit extraction.

#### Scenario: A vector PDF is submitted through different endpoints

- **WHEN** auto extraction and schematic extraction inspect the same source
- **THEN** both use identical bounded vector signals and configured limits

#### Scenario: A published profile alias is supplied

- **WHEN** the alias is accepted by an explicit adapter
- **THEN** automatic extraction accepts the same alias and resolves the same domain

### Requirement: Graph extraction is observable as an optional capability

Graph extraction SHALL expose additive capability and readiness metadata without
becoming a document-domain routing target.

#### Scenario: Graph extraction is disabled

- **WHEN** operators query capabilities or adapter readiness
- **THEN** graph extraction is reported unavailable without failing document readiness
