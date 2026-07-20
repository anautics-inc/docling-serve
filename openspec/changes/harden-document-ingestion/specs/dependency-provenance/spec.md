# Spec Delta: Dependency Provenance

## ADDED Requirements

### Requirement: Runtime dependencies are current and compatible

Direct dependencies SHALL resolve to the latest verified-compatible stable
release for every supported platform group. Compatibility exceptions SHALL
record the upstream constraint, tested version, and review condition.

#### Scenario: Latest release is incompatible on one platform

- **WHEN** the absolute latest release violates a supported platform constraint
- **THEN** the lock retains the latest compatible version and documents the exception

### Requirement: Coherent dependency families upgrade together

Docling, OpenTelemetry, Torch/torchvision, boto3/botocore, and other coupled
families SHALL be resolved and verified as coherent sets.

#### Scenario: Docling Jobkit raises its Slim requirement

- **WHEN** Jobkit is upgraded
- **THEN** Slim, Core, Graph, namespace overrides, and extraction contracts are verified together

### Requirement: Build inputs are immutable and verifiable

Production container bases, CI actions, downloaded archives, and installer
tools SHALL be pinned to immutable digests, commits, or checksums.

#### Scenario: A source archive is replaced upstream

- **WHEN** downloaded bytes do not match the recorded checksum
- **THEN** the build fails before compiling or executing them

### Requirement: Production images remain offline

All required conversion, OCR, tokenizer, and enrichment artifacts SHALL be
baked at build time. Runtime network access SHALL NOT be enabled to compensate
for a missing artifact.

#### Scenario: Baked model is missing

- **WHEN** an enabled offline capability lacks its artifact
- **THEN** readiness reports that capability unavailable without attempting a download
