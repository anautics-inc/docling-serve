# Spec Delta: Pytology–Docling contract

## ADDED Requirements

### Requirement: Every Pytology topology resolves a callable Docling base URL

Pytology SHALL resolve standalone, proxied, and embedded Docling deployments to
the namespace at which the Docling routes are actually mounted.

#### Scenario: Embedded Docling is selected

- **WHEN** a Pytology worker submits canonical work through the embedded
  topology
- **THEN** its base URL includes the `/docling` mount and the request reaches
  `/v1/chunk/{strategy}/file/async` rather than a Pytology stub or 404

### Requirement: Canonical ingestion has one active typed-extraction path

Pytology SHALL consume typed routing and artifacts from the canonical task
result and SHALL NOT retain an independently callable legacy deep-bundle path.

#### Scenario: A typed document completes

- **WHEN** the canonical result contains typed routing and artifact metadata
- **THEN** Pytology publishes and projects those artifacts without issuing a
  second synchronous typed extraction request

### Requirement: Schematic actions preserve artifact location

Every Pytology schematic check, revise, and simulate call SHALL send both the
tenant-scoped artifact prefix and its approved bucket.

#### Scenario: The configured artifact bucket differs from Docling's default

- **WHEN** a document schematic action is requested
- **THEN** Docling reads the exact Pytology-owned bucket and prefix rather than
  silently falling back to another bucket

### Requirement: Optional image context is capability-driven

Pytology SHALL call a Docling image-context route only when that route is
advertised by the deployed capability contract.

#### Scenario: Docling has no image-context endpoint

- **WHEN** `/v1/images/context` is absent from the deployed OpenAPI contract
- **THEN** Pytology reports the capability unavailable and does not present
  PDF or presentation image context as validated

### Requirement: Assertions bind every machine request

Every protected submit, poll, result, graph, typed extraction, and schematic
request SHALL mint a distinct assertion bound to the exact method, path,
tenant, document, client, audience, expiry, and replay identifier.

#### Scenario: A canonical task is polled and read

- **WHEN** Pytology polls status and then fetches the result
- **THEN** each route receives a new assertion whose resource matches that
  route exactly

### Requirement: Pytology accepts canonical results only

Pytology SHALL reject a successful task response unless it conforms to
`docling.canonical-ingestion.v1`.

#### Scenario: RQ reports success before canonical decoration

- **WHEN** Pytology observes the documented RQ publication handoff
- **THEN** it continues polling within the task deadline and never forwards the
  undecorated result downstream

### Requirement: Completion requires downstream evidence

Pytology SHALL report completion only after required entity extraction,
OpenSearch indexing, and Neo4j document/chunk projection have durable evidence.

#### Scenario: Conversion succeeds but projection fails

- **WHEN** Docling returns valid chunks and Neo4j projection lacks document or
  chunk evidence
- **THEN** the Pytology job fails or remains retryable rather than completing
