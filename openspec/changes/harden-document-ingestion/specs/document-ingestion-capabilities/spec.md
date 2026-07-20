# Spec Delta: Document Ingestion Capabilities

## ADDED Requirements

### Requirement: Every supported document family has a declared capability

The service SHALL expose one authoritative capability registry that declares
accepted extensions and media types, OCR policy, runtime dependencies,
extraction behavior, and output contract for every supported document family.

#### Scenario: Operator inspects format support

- **WHEN** an operator queries adapter readiness
- **THEN** the response identifies each document capability and whether it is available

#### Scenario: Unsupported input is submitted

- **WHEN** a source matches no enabled capability
- **THEN** admission fails with a stable policy error before expensive processing

### Requirement: Automatic routing is service-owned and deterministic

Automatic typed extraction SHALL be selected by Docling Serve from bounded
source signals and SHALL return the selected domain and reason. Clients SHALL
NOT maintain copied content-extraction heuristics.

#### Scenario: Explicit profile is supplied

- **WHEN** a supported explicit extraction profile is supplied
- **THEN** that profile wins over automatic probes

#### Scenario: Generic PDF has no typed signals

- **WHEN** a PDF has no XFA, technical-order, or schematic signals
- **THEN** it remains a generic document and no typed extractor runs

### Requirement: OCR policy is explicit and compatible

The service SHALL support `auto`, `always`, and `never` OCR policy. Existing
boolean conversion fields SHALL remain accepted during migration and SHALL map
deterministically to the typed policy.

#### Scenario: Digital document uses automatic OCR

- **WHEN** a document has a usable text layer and OCR policy is `auto`
- **THEN** unnecessary OCR is skipped

#### Scenario: Scanned input uses automatic OCR

- **WHEN** a supported scanned PDF or image lacks usable text and OCR policy is `auto`
- **THEN** an available configured OCR adapter is applied

### Requirement: All supported formats have contract coverage

Generic, typed-domain, and legacy document families SHALL have fixture-backed
tests for admission, extraction/chunking, metadata, stable failures, and output
contracts appropriate to the format.

#### Scenario: Dependency upgrade changes extraction behavior

- **WHEN** a dependency update changes a golden extraction contract
- **THEN** verification fails unless the contract change is reviewed and versioned
