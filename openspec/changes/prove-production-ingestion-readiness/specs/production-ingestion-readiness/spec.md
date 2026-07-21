# Spec Delta: Production ingestion readiness

## ADDED Requirements

### Requirement: Release evidence is revision-bound

Production acceptance SHALL execute against the candidate revision and SHALL
distinguish fresh conversion evidence from cached job retrieval.

#### Scenario: A live matrix reuses completed source content

- **WHEN** the submitted source resolves to an already completed canonical job
- **THEN** the run is recorded as a retrieval check and does not replace fresh
  conversion evidence

### Requirement: Required runtime capabilities fail closed

Every enabled production capability SHALL have current evidence at its assigned
release tier. An unavailable capability SHALL block release unless deployment
policy explicitly disables it.

#### Scenario: GPU hardware is unavailable

- **WHEN** GPU-backed model processing is enabled for the target deployment
- **THEN** release remains blocked until the GPU/model acceptance job passes on
  the target accelerator class

#### Scenario: KiCad is optional

- **WHEN** core schematic extraction is ready but `kicad-cli` is absent
- **THEN** readiness reports core extraction as available and KiCad export/ERC
  as unavailable without claiming those optional artifacts were validated

### Requirement: Distributed engines preserve the canonical contract

Local, RQ, and Ray SHALL produce the same canonical result and service-owned
failure semantics for the same admitted source.

#### Scenario: RQ executes a canonical task

- **WHEN** a staged canonical task is enqueued through Redis/RQ
- **THEN** staging materialization, telemetry, canonical decoration, result TTL,
  failure TTL, and cleanup are observable

#### Scenario: Ray executes a canonical task

- **WHEN** the Ray coordinator processes a canonical task
- **THEN** page slicing, retries, typed decoration, failure publication, and
  result shape match the Local and RQ contracts

### Requirement: Pytology uses request-bound assertions correctly

Pytology SHALL mint a new assertion for every authenticated Docling request and
bind it to the exact method, path, tenant, document, audience, client, expiry,
and replay identifier.

#### Scenario: Pytology polls and reads a task result

- **WHEN** Pytology submits, polls, reads, and clears a canonical task
- **THEN** each request uses a distinct assertion bound to that exact route and
  the canonical result is validated before downstream processing

### Requirement: End-to-end completion includes downstream checkpoints

A Pytology document job SHALL NOT report completion until required chunking,
entity extraction, OpenSearch indexing, and Neo4j projection checkpoints pass.

#### Scenario: Any downstream checkpoint fails

- **WHEN** conversion succeeds but NER, indexing, or graph projection fails
- **THEN** the job remains failed or retryable and does not report canonical
  production completion

### Requirement: Credentialed staging proves cloud controls

Required S3 staging SHALL prove TLS, fixed prefix, KMS enforcement, object
integrity metadata, lifecycle policy, canary operations, and cleanup using the
target deployment identity.

#### Scenario: Staging policy is incomplete

- **WHEN** any required bucket, region, KMS, lifecycle, integrity, or cleanup
  check is unavailable
- **THEN** readiness and release acceptance fail closed
