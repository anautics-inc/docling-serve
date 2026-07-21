# Tasks: Prove production ingestion readiness

## 1. Specification and inventory

- [x] 1.1 Add proposal, design, requirement deltas, and traced tasks.
- [x] 1.2 Strictly validate the OpenSpec change.
- [x] 1.3 Reconcile every release gate with the path coverage ledger.

## 2. Distributed and external runtimes

- [x] 2.1 Execute Redis/RQ lifecycle validation.
- [x] 2.2 Execute Ray parity and coordinator validation.
- [x] 2.3 Execute credentialed S3/KMS staging validation.
- [x] 2.4 Validate GPU/model availability and enforce its release tier.
- [x] 2.5 Validate KiCad policy and readiness semantics.

## 3. Cross-service contract

- [x] 3.1 Audit every Pytology call to Docling Serve.
- [x] 3.2 Validate assertion, tenant, document, polling, result, and cleanup calls.
- [x] 3.3 Validate fresh all-format ingestion through search, NER, and Neo4j.
- [x] 3.4 Record all remaining Pytology-owned work in one handoff file.

## 4. Production acceptance

- [x] 4.1 Run production image, external-runtime, SBOM, and startup gates.
- [x] 4.2 Run production-sample verification or block the release tier explicitly.
- [x] 4.3 Run full quality, OpenSpec, Graphify, and hermetic suites.
- [x] 4.4 Publish an evidence-backed production readiness decision.
