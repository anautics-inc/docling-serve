# Spec Delta: Ingestion code reduction

## ADDED Requirements

### Requirement: Dead and migration-complete layers are removed

Modules without production or documented compatibility consumers SHALL be
deleted, and internal callers SHALL use canonical package exports.

#### Scenario: A compatibility shim has only test consumers

- **WHEN** tests can import the upstream canonical type directly
- **THEN** the shim is removed and tests migrate to the canonical import

### Requirement: Shared behavior has one implementation

Typed bundle publication, common request validation, staged task lifecycle,
and KiCad export behavior SHALL each have one implementation reused by their
transports.

#### Scenario: Explicit and automatic typed extraction run

- **WHEN** both select the same domain and source
- **THEN** they invoke the same domain service and publication helper

### Requirement: Reductions preserve capability contracts

Code or file reduction SHALL NOT remove an admitted format, public HTTP path,
bundle schema, tenant boundary, or deployment engine.

#### Scenario: Architecture metrics improve

- **WHEN** files or duplicated logic are removed
- **THEN** capability, OpenAPI, worker parity, and bundle contract tests still
  pass before ratchet ceilings are lowered
