# PRD: Pedagogical Metadata Layer — AI Course Reengineering Engine

**Date:** 2026-05-20
**Owner:** Captify platform team
**Phase:** 4 (PRD) — see `.specify/` 7-phase workflow
**Source idea:** `context.md` in this folder
**Companion specs:** `data-model.md` (the `course-model.json` contract), `tasks.md` (Phase-5 kanban)

---

## 1. Problem Statement

The current extraction pipeline (experiment6) turns PPTX/DOCX/XLSX/PDF into a
structural manifest — geometry, text, typography, assets, speaker notes, a
deterministic Bloom scaffold, and a viewable preview. That manifest is
*rendering-grade*: it answers "what is on the slide and where."

It does not answer the **instructional** questions a course redesign needs:
What is this course teaching? What are the learning objectives? Does the
instruction support the task the learner must perform? Is the sequence sound?
Where is the deck overloaded, redundant, or missing practice and feedback?

Without that pedagogical model, a downstream slide-reengineering agent has no
structured instructional truth to reason over — it would have to re-derive all
of it per request, inconsistently.

## 2. Solution Overview

Add a new, **read-only-on-the-source** processing stage: the **Course
Preparation / Pedagogical Metadata Layer**. It consumes the existing
extraction manifest and produces a pedagogical course model.

This stage does five things — **enrich, classify, organize, normalize,
prepare** — and explicitly does NOT rebuild or rewrite slides. Its output is
the structured course model handed to a later pedagogical reengineering agent.

**It emits three new artifacts** (alongside, never replacing, the manifest):

| Artifact | Purpose |
| --- | --- |
| `course-model.json` | Canonical pedagogical representation — course, modules, objectives, per-slide instructional metadata, assessments, sequencing + overload analysis, reengineering candidates |
| `course-analysis-summary.json` | Lightweight roll-up — pedagogical scores, risks, readiness, rebuild priority, module summaries — for downstream orchestration |
| `reengineering-input.json` | Minimal artifact for the redesign agent — only slides flagged for review, plus their objectives, issues, rationale, module + sequencing context |

It also adds a `pedagogical` block onto each slide/unit record (additive — see §6).

## 3. Input Contract

**Input:** the current extraction manifest — one per source document. This
stage **does not re-parse the source file**; the manifest is the source of
truth for geometry, text, assets, notes, and rendering.

> **Assumption to confirm (A1).** `context.md` names the input
> `pptx-ooxml-geometry.json`. The experiment6 pipeline emits the equivalent
> data as `manifest.json` (schemaVersion 3.0). This PRD treats them as the
> same artifact and refers to it as the **extraction manifest**. Confirm the
> deployed filename before implementation; the reader must accept whatever the
> extraction stage actually produces.

Fields this stage relies on (present in the experiment6 manifest):

- `units[]` — `unitId`, `unitType` (slide/page/section), `index`, `pageNumber`,
  `title`, `pageSizeEmu`, `blocks[]`, `speakerNotes`, `classification`,
  `background`
- `units[].blocks[]` — `blockId`, `kind` (text/table/picture), `text`,
  `paragraphs` (run-level typography), `bbox`, `doclingLabel`, `readingOrder`,
  `classification`
- `units[].speakerNotes` — `raw`, `cleaned`
- `assets[]` — picture assets with `caption` (vision-model caption when present)
- `taxonomy` — the existing deterministic Bloom scaffold (consumed, then
  superseded by this stage's richer objective/Bloom model)
- `theme`, `diagnostics`, `source`, `documentId`

If a manifest field is missing (e.g. a non-PPTX format with no `speakerNotes`
content), the stage degrades gracefully — it does not fail.

## 4. Scope

**In scope:** instructional classification, course-structure inference,
learning-objective extraction (incl. Air Force task/condition/standard),
objective alignment, Bloom appropriateness, Gagné sequencing analysis,
assessment detection, content density + redundancy detection, reengineering
*candidate identification*, and emitting the three artifacts.

**Out of scope** (see §12 for the full list): rebuilding or rewriting any
slide, mutating geometry, moving slides, generating new slide content, the
downstream reengineering agent itself.

## 5. Architecture

### 5.1 Module / package

A new package — proposed `course_model/` — consuming the manifest. It does not
import or modify the experiment6 `deep_document/` extraction code; it depends
only on the manifest contract.

> **Assumption to confirm (A2).** Project convention has used
> `tests/experimentN/` iteration folders. This PRD assumes the pedagogical
> layer is built as `course_model/` (a peer package) so it is reusable by the
> production API, not buried in an experiment folder. Confirm placement.

### 5.2 Provider strategy — deterministic first, LLM for inference

Per **Agent Rule 3** ("deterministic where possible; confidence scoring where
inference is used"), the stage reuses the experiment6 provider-abstraction
pattern. Every layer has:

- a **deterministic provider** — structural/heuristic signals, runs with no
  network, every output confidence-scored; and
- a **Bedrock provider** — genuine pedagogical inference (objective phrasing,
  task/condition/standard parsing, Gagné mapping, Bloom appropriateness),
  fail-open to the deterministic provider, env-selected, fail-closed on
  missing config (carry the experiment6 `provider_config` fail-closed rule
  forward).

Cheap structural detection (section dividers, density, redundancy, assessment
keyword hits, agenda patterns) is deterministic. Genuine pedagogical judgment
(what is the objective, is the instruction sufficient for the task) uses the
LLM. **Both paths emit `confidence` and `provider` on every inference.**

### 5.3 The twelve-step pipeline

`context.md` defines the implementation sequence. Each step is one layer/module:

| # | Layer | Module | Output model |
| --- | --- | --- | --- |
| 1 | Load manifest | `course_model_builder` | — |
| 2 | Course metadata inference | `course_metadata` | `CourseMetadata` |
| 3 | Module inference | `module_inference` | `Module[]` |
| 4 | Objective extraction | `objective_extraction` | `LearningObjective[]` |
| 5 | Slide instructional classification | `slide_classification` | `SlideInstructionalMetadata` |
| 6 | Objective alignment (AF task/condition/standard) | `objective_alignment` | `ObjectiveAlignment[]` |
| 7 | Bloom appropriateness | `bloom_analysis` | `BloomAnalysis[]` |
| 8 | Gagné sequencing | `gagne_sequencing` | `ModuleInstructionSequence[]` |
| 9 | Assessment detection | `assessment_detection` | `Assessment[]` |
| 10 | Density + redundancy detection | `density_analysis` | `SlideDensityAnalysis[]`, `RedundancyAnalysis[]` |
| 11 | Reengineering candidate detection | `reengineering` | `ReengineeringCandidate[]` |
| 12 | Emit artifacts | `course_model_builder` | the 3 output files |

Steps 2–11 each consume the manifest plus prior layers' output; step 12
assembles and writes. The full field-level contract for every model is in
`data-model.md`.

## 6. Output Artifacts

### 6.1 `course-model.json` — canonical model

Top-level structure: `course` { `metadata`, `modules[]`, `slides[]`,
`objectives[]`, `assessments[]`, `pedagogicalAnalysis`,
`reengineeringCandidates[]` }. See `data-model.md`.

### 6.2 Per-slide `pedagogical` extension (additive)

Each slide record gains a `pedagogical` block — the existing geometry/text/
asset/format fields are **untouched**:

```jsonc
"pedagogical": {
  "role": ["Concept", "Definition"],
  "moduleId": "module-002",
  "inferredObjectiveIds": ["obj-004"],
  "bloomSignal": "understand",
  "densityScore": 0.71,
  "overloadRisk": "medium",
  "assessmentSignal": false,
  "retrievalSignal": false,
  "feedbackSignal": false,
  "instructionalValue": "high",
  "redundancyRisk": "low",
  "reengineeringCandidate": false
}
```

### 6.3 `course-analysis-summary.json` and `reengineering-input.json`

The summary carries pedagogical scores, risks, readiness, rebuild priority,
and per-module summaries. The reengineering input carries **only slides
flagged for review** plus their objectives, issues, rationale, and module +
sequencing context — it is the minimal hand-off to the redesign agent.

## 7. User Stories

1. As an instructional designer, I want each slide classified by instructional
   role, so I can see what the deck actually teaches versus just presents.
2. As an instructional designer, I want learning objectives extracted (explicit
   and inferred), so I can audit whether the course has a defined destination.
3. As an Air Force training reviewer, I want each objective modeled as
   task / condition / standard, so I can judge performance readiness — not just
   academic Bloom level.
4. As a training reviewer, I want objective alignment scored — does the
   instruction support the task, does the assessment verify it — so coverage
   gaps are explicit.
5. As a training reviewer, I want Bloom appropriateness flagged only when it is
   *wrong for the task*, so a correctly Apply-level procedural course is not
   penalized for "low" Bloom.
6. As a training reviewer, I want each module mapped against Gagné's nine
   events, so missing instructional events (no practice, no feedback) surface.
7. As a course manager, I want overloaded and redundant slides detected, so
   rework can be prioritized.
8. As a course manager, I want a course-analysis summary with readiness scores
   and a rebuild priority, so I can triage many decks.
9. As the downstream reengineering agent, I want a minimal input artifact
   containing only the slides needing review with rationale and context, so I
   redesign precisely instead of reprocessing the whole deck.
10. As a compliance reviewer, I want every inference traceable to source slide
    IDs with a confidence score, so pedagogical claims are auditable.
11. As a developer, I want the stage to never mutate the extraction manifest's
    geometry/text, so the manifest stays the rendering source of truth.

## 8. Pedagogical Rules (non-negotiable)

These are the `context.md` Agent Rules, carried into the contract:

- **R1 — Geometry is source of truth.** Never mutate layout coordinates,
  text, or assets. The `pedagogical` block is purely additive.
- **R2 — No rewriting in this phase.** Classify and prepare only; identify
  reengineering *candidates*, never produce new slide content.
- **R3 — Deterministic where possible; confidence-scored where inferred.**
- **R4 — Source traceability.** Every inference maps back to source slide IDs.
- **R5 — Air Force instructional model overrides generic Bloom.** The primary
  question is "can the learner perform the required task under defined
  conditions to standard?" — not "what Bloom level is this?"
- **R6 — Higher Bloom is not better.** A procedural course correctly stays at
  Apply; only flag Bloom that is *wrong for the task*.
- **R7 — Use speaker notes heavily.** Notes carry instructor intent, hidden
  objectives, practice guidance, and assessment context.

## 9. Acceptance Criteria

The stage is done when, for every input manifest:

- [ ] No existing extraction data is lost or mutated — geometry/text/assets/
      notes byte-identical before and after; a regression test proves it.
- [ ] Every slide is classified — has a non-empty `pedagogical.role`.
- [ ] Modules are inferred, each preserving original slide references (no slide
      is moved or renumbered).
- [ ] Objectives are extracted; objectives not explicitly stated are emitted
      with `type: "Inferred"` and a `confidence` < 1.0.
- [ ] Air Force objectives have `task`, `condition`, `standard` populated when
      that pattern is detected.
- [ ] Each objective has an `ObjectiveAlignment` with instruction/assessment
      support scores.
- [ ] Each objective has a `BloomAnalysis` with a `mismatchType` from the
      allowed set; procedural Apply-level objectives are not flagged
      `underBloomed`.
- [ ] Each module has a `ModuleInstructionSequence` with Gagné coverage.
- [ ] Assessments are detected and linked to the objectives they measure.
- [ ] Density and redundancy are scored per slide.
- [ ] Reengineering candidates are identified (candidates only — no rewrites).
- [ ] All three artifacts (`course-model.json`, `course-analysis-summary.json`,
      `reengineering-input.json`) are emitted and validate against a published
      JSON Schema.
- [ ] `reengineering-input.json` contains only slides flagged for review.
- [ ] Every inference carries `confidence` and `provider`; deterministic-only
      runs complete with no network.

## 10. Implementation Notes

- Reuse the experiment6 provider abstraction (`provider_from_environment`,
  fail-open Bedrock, `provider_config` fail-closed on missing model/region,
  `usage_accounting` for token/cost telemetry). The pedagogical LLM calls must
  appear in `manifest.usage` / a usage block — production needs the cost.
- Build deterministic layers first; they are the baseline and the test
  oracle. Wire the Bedrock provider behind the same env switch pattern.
- Slide-role classification allows **multiple roles** per slide (`role[]`).
- Module inference must be order-preserving and reference-preserving (R1/R2).
- The `course-model.json` `slides[]` should reference manifest `unitId`s, not
  copy geometry — keep the model lean and the manifest authoritative.
- Emit a JSON Schema for `course-model.json` (mirror experiment6's
  `docling_schema` approach — strict, enum-checked, validated in tests).

## 11. Testing Decisions

- Golden-fixture tests: run the stage on the experiment6 corpus manifests
  (7 PPTX + DOCX + XLSX + PDF) and assert the acceptance criteria.
- A "no mutation" test: deep-compare the manifest's geometry/text/assets/notes
  before and after the stage.
- Per-layer unit tests with deterministic providers (no network).
- Schema-validation test for all three output artifacts.
- A traceability test: every objective/finding references a real `unitId`.
- An Air Force objective test: a fixture objective in
  "Perform X IAW TO under Y conditions to Z standard" form yields populated
  `task` / `condition` / `standard`.
- A Bloom-appropriateness test: a procedural Apply-level objective is NOT
  flagged `underBloomed` (R6).
- Provider tests: deterministic run records zero LLM usage; Bedrock mode fails
  closed without explicit model/region config.

## 12. Out of Scope

- Rebuilding, rewriting, or generating slide content.
- Moving, reordering, merging, or deleting slides (candidates are *identified*,
  not executed).
- Mutating geometry, layout coordinates, or any existing manifest field.
- The downstream pedagogical reengineering agent itself.
- Re-parsing the source PPTX/DOCX/XLSX/PDF.
- A production canvas/tldraw viewer.
- Fine-tuning models or training a classifier.

## 13. Open Questions / Assumptions

| # | Item | Needs |
| --- | --- | --- |
| A1 | Input filename — `pptx-ooxml-geometry.json` (context.md) vs `manifest.json` (experiment6) | Confirm the deployed name; the reader accepts the actual artifact |
| A2 | Package placement — `course_model/` peer package vs `tests/experiment7/` | Confirm where this code lives |
| A3 | Is "Air Force" audience fixed, or one of several instructional models the stage must support? | Confirm whether the AF task/condition/standard model is universal or audience-gated |
| A4 | Module inference with no section dividers / agenda — acceptable to emit one whole-course module? | Confirm fallback behavior |
| A5 | Does the existing manifest `taxonomy` (deterministic Bloom) get removed, kept, or marked superseded? | Confirm — this PRD keeps it (R1: don't break existing) and treats the course model as the authority |
| A6 | LLM cost ceiling per course for the pedagogical stage | Confirm a per-document budget (reuse experiment6 budget mechanism) |

Resolve A1–A6 before Phase-5 implementation begins; none block Phase-4 review.
