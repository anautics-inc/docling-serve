# Spec Delta: Production Ingestion Policy

## ADDED Requirements

### Requirement: Authenticated document work is tenant-scoped

Authenticated extraction, task status, and task result requests SHALL contain a
validated tenant identifier. The service SHALL NOT silently assign authenticated
work to a shared default tenant.

#### Scenario: Authenticated request omits tenant

- **WHEN** an authenticated document request omits its tenant identifier
- **THEN** the request fails before source bytes or task state are accessed

### Requirement: Unsafe exposure fails startup validation

Non-local deployments SHALL reject unauthenticated mode and wildcard CORS.
Local development exceptions SHALL require an explicit setting.

#### Scenario: Production enables unauthenticated mode

- **WHEN** production settings select `auth_mode=none`
- **THEN** service startup fails with a configuration error

### Requirement: Remote model work is explicit and bounded

Vision, drawing-twin, graph, and other remote-model work SHALL be disabled
unless explicitly enabled with configured transport, model alias, timeout,
retry, page/call/token budgets, and observable usage.

#### Scenario: LiteLLM transport exists but feature is disabled

- **WHEN** LiteLLM credentials are configured and a model-driven feature is disabled
- **THEN** the feature performs no remote model call

### Requirement: Optional adapter failure is granular

Failure of an optional adapter SHALL mark only that capability unavailable and
SHALL NOT make unrelated generic ingestion unready.

#### Scenario: LibreOffice is unavailable

- **WHEN** legacy Office conversion is enabled but its executable is unavailable
- **THEN** legacy Office capability is unavailable while PDF and DOCX remain ready

### Requirement: Legacy Office conversion is isolated and bounded

Legacy Office sources SHALL be materialized under network policy and converted
through an isolated subprocess boundary with time, input, output, scratch,
file-count, executable, and process-lifecycle controls.

#### Scenario: Converter escapes resource controls

- **WHEN** a LibreOffice child survives termination or exceeds a hard boundary
- **THEN** the worker fails closed and does not process another task
