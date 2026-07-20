# Tasks: Harden document ingestion

## 1. Specification and characterization

- [x] 1.1 [DIC-1] Add Docling Serve OpenSpec configuration and strict change artifacts.
- [x] 1.2 [DIC-4] Add a declarative all-format capability and contract matrix.
- [x] 1.3 [DIC-4] Add cross-repository client and bundle contract tests.

## 2. Capability and OCR policy

- [x] 2.1 [DIC-1] Implement the typed document capability registry.
- [x] 2.2 [DIC-2] Route explicit and automatic extraction through registered adapters.
- [x] 2.3 [DIC-3] Add typed OCR policy and compatibility translation.
- [x] 2.4 [DIC-2] Remove copied routing heuristics from Pytology.
- Blocked by: tasks 1.2 and 2.1.

## 3. Production policy

- [x] 3.1 [PIP-1] Require tenant scope for authenticated document/task operations.
- [x] 3.2 [PIP-2] Validate development-only auth and CORS exceptions at startup.
- [x] 3.3 [PIP-3] Make model-driven passes explicit, bounded, and observable.
- [x] 3.4 [PIP-4] Report optional adapter readiness independently.

## 4. Legacy isolation and cleanup

- [x] 4.1 [PIP-5] Split legacy source, sandbox, result, and integration boundaries.
- [x] 4.2 [PIP-4] Preserve `.doc/.ppt/.xls` behavior with granular readiness and smoke tests.
- [x] 4.3 Remove usage-proven dead compatibility code and stale operational guidance.

## 5. Dependency provenance

- [x] 5.1 [DP-1,DP-2] Upgrade and lock verified-compatible Python dependency families.
- [x] 5.2 [DP-3] Pin container, native source, installer, and CI inputs.
- [x] 5.3 [DP-4] Verify baked-model offline behavior and SBOM/provenance output.

## 6. Coordinated consumers

- [x] 6.1 Update Pytology client, worker, settings, lifecycle spec, and tests.
- [x] 6.2 Update Captify Core Spaces client configuration and contract tests.
- [x] 6.3 Update deployed mirrors only where parity inventory confirms use.
- [x] 6.4 Update deployment and operator documentation.

## 7. Verification

- [x] 7.1 Run `npx --yes @fission-ai/openspec@latest validate --all --strict`.
- [x] 7.2 Run ruff, mypy, targeted tests, and full Docling Serve tests.
- [x] 7.3 Run Pytology and Captify Core contract suites.
- [x] 7.4 Build and smoke the offline container, legacy adapter, and S3 publication.
