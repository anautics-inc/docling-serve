# Spec Delta: Ingestion execution boundaries

## ADDED Requirements

### Requirement: All authenticated uploads share admission policy

Generic and typed uploads SHALL apply the same tenant validation, actual-byte
limits, required staging, materialization, audit identity, and cleanup policy.

#### Scenario: Production staging is required

- **WHEN** a typed extract endpoint receives a file
- **THEN** it stages and materializes the source through the same tenant-scoped
  boundary as asynchronous conversion

#### Scenario: Admission fails after partial staging

- **WHEN** any source in a request fails validation or staging
- **THEN** already staged sources are cleaned without enqueueing or extracting

### Requirement: Extraction services are transport independent

Domain extraction and artifact publication SHALL be callable without FastAPI
route objects and SHALL receive admitted sources and narrow dependencies.

#### Scenario: Automatic extraction selects a typed adapter

- **WHEN** the registry resolves a domain
- **THEN** the route calls the adapter service rather than another route handler

### Requirement: Worker engines share one task lifecycle

Local, RQ, and Ray SHALL use one materialize, execute, public-error mapping, and
cleanup envelope while retaining engine-specific transport behavior.

#### Scenario: Conversion fails after materialization

- **WHEN** any engine observes the same conversion failure
- **THEN** public failure data, tenant metadata, cleanup state, and retryability
  are equivalent

### Requirement: Moved internals preserve compatibility

Physical module splits SHALL retain reviewed import facades and stable dynamic
entrypoints until all callers migrate.

#### Scenario: An existing caller imports a moved symbol

- **WHEN** it uses a documented compatibility path
- **THEN** it resolves the same object or behavior during the migration window
