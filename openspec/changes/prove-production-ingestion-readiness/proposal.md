# Proposal: Prove production ingestion readiness

## Why

Hermetic and container validation covers every Docling Serve path, but final
release evidence still depends on distributed runtimes, credentialed storage,
model hardware, request-bound assertions, production samples, and the Pytology
consumer. A production release must distinguish executed evidence from
scheduled evidence and fail closed when a required tier is unavailable.

## What Changes

- Define one release-evidence contract for Local, RQ, Ray, GPU/model, S3/KMS,
  KiCad, container, and post-deploy validation.
- Exercise Pytology's canonical submission, assertion, polling, result,
  cleanup, search, NER, and Neo4j checkpoints against Docling Serve.
- Make optional runtime capabilities explicit instead of silently degrading.
- Record remaining consumer-owned work in `captify-pytology-todos.md`.
- Require every critical release gate to be executed or carry an explicit,
  machine-enforced deployment prohibition.

## Capabilities

### New Capabilities

- `production-ingestion-readiness`
- `pytology-docling-contract`

### Modified Capabilities

- `format-matrix-verification`
- `ingestion-execution-boundaries`
- `production-ingestion-policy`

## Compatibility

- Preserve current HTTP routes, canonical result schema, typed bundle schemas,
  tenant headers, assertion claims, and Local/RQ/Ray engine selection.
- Optional KiCad outputs remain optional unless deployment policy requires
  export or ERC.
- GPU, credentialed, and distributed tests remain outside hermetic merge CI but
  become mandatory release-tier jobs when their capability is enabled.

## Non-goals

- Emulating unavailable GPU hardware in software.
- Treating cached document jobs as fresh live-conversion evidence.
- Moving Pytology-owned search, NER, graph, or IAM behavior into Docling Serve.
