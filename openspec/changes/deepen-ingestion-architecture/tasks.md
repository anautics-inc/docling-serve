# Tasks: Deepen ingestion architecture

## 1. Specification and evidence

- [x] 1.1 [EIA-1] Add proposal, design, requirement deltas, and traced tasks.
- [x] 1.2 [IAR-1,IAR-2] Add pinned Graphify snapshot, baseline, and ratchet tooling.
- [x] 1.3 [IAR-1] Strictly validate the change before runtime edits.

## 2. Executable adapters

- [x] 2.1 [EIA-1] Add executable adapter protocol and registry.
- [x] 2.2 [EIA-2] Unify profile aliases and bounded PDF signals.
- [x] 2.3 [EIA-1,EIA-3] Delegate readiness and auto extraction to the registry.
- [x] 2.4 [EIA-3] Add optional graph capability readiness and behavior tests.

## 3. Admission and HTTP boundaries

- [x] 3.1 [IEB-1] Add shared tenant-scoped upload admission.
- [x] 3.2 [IEB-2] Move generic artifact publication to a neutral service.
- [x] 3.3 [IEB-2] Add transport-independent extraction services.
- [x] 3.4 [IEB-2] Extract domain routers and slim `create_app()`.

## 4. Worker execution

- [x] 4.1 [IEB-3] Centralize converter and engine config builders.
- [x] 4.2 [IEB-3] Add one staged-task execution and failure envelope.
- [x] 4.3 [IEB-3] Migrate Local, RQ, and Ray with parity tests.

## 5. Cycle and hotspot removal

- [x] 5.1 [IAR-2] Break the schematic extraction/revision cycle.
- [x] 5.2 [IEB-4] Split staging internals behind a facade.
- [x] 5.3 [IEB-4] Split legacy Office internals behind a facade.
- [x] 5.4 [IEB-4] Add narrow settings views while preserving flat env aliases.
- [x] 5.5 [IEB-4] Normalize package exports and gate deprecated shims.

## 6. Verification

- [x] 6.1 [IAR-2] Ratchet SCC, fan-out, fan-in, and hotspot ceilings.
- [x] 6.2 Run ruff, mypy, full hermetic tests, and wheel import smoke.
- [x] 6.3 Run Pytology receiver/client contract tests.
- [x] 6.4 Build and smoke the offline container, legacy adapter, ngspice, and SBOM.
- [x] 6.5 Run strict OpenSpec and Graphify provenance/ratchet gates.
