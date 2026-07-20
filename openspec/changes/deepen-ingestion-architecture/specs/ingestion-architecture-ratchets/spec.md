# Spec Delta: Ingestion architecture ratchets

## ADDED Requirements

### Requirement: Architecture evidence is reproducible

Architecture checks SHALL use the lockfile-pinned Graphify distribution against
a content-addressed snapshot of tracked and non-ignored working-tree files.

#### Scenario: Source changes after graph generation

- **WHEN** graph provenance does not match the current source snapshot
- **THEN** the architecture gate fails and requires a fresh scan

### Requirement: Coupling metrics cannot regress

Production-only strongly connected components, configuration fan-in, file
fan-out, dangling ratio, and named hotspot concentration SHALL be independently
bounded by reviewed ceilings.

#### Scenario: A refactor adds an import cycle

- **WHEN** a production SCC exceeds the recorded ceiling
- **THEN** verification fails even if total node and edge counts decrease

#### Scenario: A hotspot is decomposed

- **WHEN** its reviewed coupling or concentration metric decreases
- **THEN** the ceiling is ratcheted downward after behavior tests pass

### Requirement: Compatibility gates accompany architecture gates

Architecture improvements SHALL NOT be accepted unless HTTP, bundle, tenant,
worker, settings, client, and offline-container contracts also pass.

#### Scenario: Graph metrics improve but a route changes

- **WHEN** OpenAPI or response-contract verification detects drift
- **THEN** the change fails unless a versioned migration is specified
