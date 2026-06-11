# Tasks: Pedagogical Metadata Layer (Phase 5 — Kanban)

**Companion to:** `prd.md`, `data-model.md` in this folder.
**Branching:** one feature branch per ticket where practical; commit only on green.

Each ticket: **inputs → outputs → done-state**. Build the **deterministic
provider first** in every layer ticket (it is the baseline and the test
oracle); wire the Bedrock provider behind the env switch second. Every
inference emits `confidence` + `provider` (PRD R3).

Resolve PRD open questions **A1–A6 before starting T1**.

---

## T0 — Scaffold `course_model/` package + provider abstraction
**Blocks:** all.
- Create the `course_model/` package (placement per A2).
- `manifest_loader.py` — load + lightly validate the extraction manifest;
  expose units/blocks/notes/assets/taxonomy. Never re-parse the source file.
- `pedagogy_provider.py` — `DeterministicPedagogyProvider` +
  `BedrockPedagogyProvider`, `provider_from_environment()`, fail-open + the
  experiment6 `provider_config` fail-closed rule for missing model/region.
- Wire `usage_accounting` (reuse experiment6's) for LLM token/cost telemetry.
- **Done:** package imports clean; a no-op pipeline loads a manifest and
  round-trips it unchanged; provider selection has a test.

## T1 — Course metadata inference (pipeline step 2)
**Inputs:** manifest. **Blocked by:** T0.
- `course_metadata.py` → `CourseMetadata` (instruction type, audience,
  delivery mode, contains-* flags).
- **Done:** every corpus manifest yields a `CourseMetadata` with a valid
  `detectedInstructionType` enum and confidence; unit test.

## T2 — Module inference (step 3)
**Inputs:** manifest. **Blocked by:** T0.
- `module_inference.py` → `Module[]` from section dividers, title/topic/layout
  shifts, note transitions, terminology clusters, agenda patterns.
- **Order- and reference-preserving** (PRD R1/R2); fallback per A4.
- **Done:** modules cover every slide exactly once; `startSlide`/`endSlide`/
  `slideIds` reference real `unitId`s; test asserts no slide moved.

## T3 — Objective extraction (step 4)
**Inputs:** manifest, modules (T2). **Blocked by:** T2.
- `objective_extraction.py` → `LearningObjective[]` — explicit (slide + notes)
  and inferred; Air Force task/condition/standard parsing.
- Inferred objectives get `type:"Inferred"`, `confidence < 1.0`.
- **Done:** AF-form objective fixture yields populated task/condition/standard;
  every objective references `supportingSlides`; test.

## T4 — Slide instructional classification (step 5)
**Inputs:** manifest, modules (T2). **Blocked by:** T2.
- `slide_classification.py` → `SlideInstructionalMetadata` per unit; `role[]`
  multi-valued from the allowed role set.
- **Done:** every slide classified (non-empty `role`); test on the corpus.

## T5 — Objective alignment (step 6, Air Force model)
**Inputs:** objectives (T3), slide classification (T4). **Blocked by:** T3, T4.
- `objective_alignment.py` → `ObjectiveAlignment[]` — instruction/assessment
  support, task/condition/standard coverage, performance-readiness risk.
- **Done:** every objective has an alignment record with findings traced to
  `unitId`s; test.

## T6 — Bloom appropriateness (step 7)
**Inputs:** objectives (T3), slide + assessment signals. **Blocked by:** T3.
- `bloom_analysis.py` → `BloomAnalysis[]` — objective/instruction/assessment
  Bloom, `mismatchType` from the allowed set.
- **R6:** a procedural Apply-level objective must NOT be flagged `underBloomed`.
- **Done:** test proves the R6 rule; every objective has a `BloomAnalysis`.

## T7 — Gagné sequencing (step 8)
**Inputs:** modules (T2), slide classification (T4), assessments (T8).
**Blocked by:** T2, T4, T8.
- `gagne_sequencing.py` → `ModuleInstructionSequence[]` — 9-event coverage,
  missing/weak events, integrity score. Classify only — no rewrite.
- **Done:** every module has a sequence record; missing events listed; test.

## T8 — Assessment detection (step 9)
**Inputs:** manifest, slide classification (T4). **Blocked by:** T4.
- `assessment_detection.py` → `Assessment[]` — quizzes, knowledge checks,
  scenario/discussion prompts, practical checks, embedded questions.
- Link each assessment to the objectives it measures.
- **Done:** assessments detected + objective-linked on the corpus; test.

## T9 — Density + redundancy detection (step 10)
**Inputs:** manifest. **Blocked by:** T0 (deterministic; manifest-only).
- `density_analysis.py` → `SlideDensityAnalysis[]` (deterministic metrics) +
  `RedundancyAnalysis[]`.
- **Done:** every slide has a density score; duplicate-concept fixture flags
  a redundancy record; test.

## T10 — Reengineering candidate detection (step 11)
**Inputs:** T5, T6, T7, T9. **Blocked by:** T5, T6, T7, T9.
- `reengineering.py` → `ReengineeringCandidate[]` — candidates ONLY, from the
  allowed type set, each with reason/issue/priority/confidence.
- **Done:** candidates reference real `unitId`s; no slide content generated;
  test asserts candidates-only.

## T11 — Orchestrator + emit artifacts (step 12)
**Inputs:** all layers. **Blocked by:** T1–T10.
- `course_model_builder.py` — run steps 1–11, assemble and emit
  `course-model.json`, `course-analysis-summary.json`,
  `reengineering-input.json`.
- `reengineering-input.json` contains **only** slides flagged for review.
- **Done:** all three artifacts emitted for every corpus manifest; a runner
  script processes the corpus and writes a summary.

## T12 — Additive per-slide `pedagogical` block
**Inputs:** slide classification (T4) + analyses. **Blocked by:** T4, T11.
- Write the `pedagogical` block onto each manifest slide record — additive;
  geometry/text/assets/format untouched.
- **Done:** "no mutation" regression test (T14) passes with this in place.

## T13 — JSON Schema + validation
**Blocked by:** T11.
- Strict Draft 2020-12 schema for the three artifacts (enum-checked roles/
  types/mismatch values, required `confidence`+`provider`, top-level
  `additionalProperties:false`).
- **Done:** every emitted artifact validates; schema-validation test.

## T14 — Regression + golden-fixture tests
**Blocked by:** T11, T12.
- No-mutation test: manifest geometry/text/assets/notes byte-identical
  before/after the stage.
- Golden-fixture run across the experiment6 corpus asserting every PRD §9
  acceptance criterion.
- Traceability test: every objective/finding references a real `unitId`.
- Provider tests: deterministic run records zero LLM usage; Bedrock mode
  fails closed without explicit model/region config.
- **Done:** full suite green.

## T15 — QA plan (Phase 7)
**Blocked by:** T14.
- Write `qa.md` — human-executable scenarios per layer, expected outcomes,
  edge cases (deck with no objectives, no modules, no assessments;
  non-PPTX manifest).
- **Done:** `qa.md` committed.

---

## Dependency graph

```
T0
├── T1
├── T2 ──┬── T3 ──┬── T5 ──┐
│        │        └── T6 ──┤
│        └── T4 ──┬── T8 ──┴── T7 ──┐
│                 └────────────────┤
├── T9 ───────────────────────────┤
│                                  └── T10 ── T11 ── T12 ── T14 ── T15
└──────────────────────────────────────────── T13 ──┘
```

Critical path: **T0 → T2 → T4 → T8 → T7 → T10 → T11 → T14**.
