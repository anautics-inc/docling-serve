# Spec Delta: Format matrix verification

## ADDED Requirements

### Requirement: Every admitted extension has canonical contract coverage

A reviewed corpus SHALL map every admitted extension to a valid source,
expected domain, canonical result contract, and downstream checkpoint
expectation.

#### Scenario: A new extension is admitted

- **WHEN** the capability registry gains an extension
- **THEN** verification fails until the corpus and expected routing are updated

### Requirement: Hermetic tests and live tests have explicit scopes

Hermetic verification SHALL prove admission, routing, task envelopes, result
normalization, and downstream orchestration. Environment-gated verification
SHALL prove real conversion and live storage/graph effects.

#### Scenario: Live infrastructure is unavailable

- **WHEN** default CI runs without Docling, OpenSearch, or Neo4j endpoints
- **THEN** live matrix tests are explicitly skipped and hermetic coverage still
  validates every format contract

#### Scenario: The live matrix runs

- **WHEN** integration infrastructure and corpus fixtures are configured
- **THEN** each required family produces chunks retrievable from OpenSearch and
  document/chunk projection evidence in Neo4j

### Requirement: Tests are independently collectable

Tests SHALL NOT import helpers from other test modules whose collection order
changes import resolution.

#### Scenario: A worker test file runs alone

- **WHEN** only that file is selected
- **THEN** its fixtures and helpers resolve without collecting another test
  module first
