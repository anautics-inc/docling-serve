# Data Model: `course-model.json` and companion artifacts

**Companion to:** `prd.md` in this folder.
**Status:** spec — the contract the Phase-5 implementation must satisfy.

This document is the field-level contract. Conventions:

- Every **inferred** value carries `confidence` (0.0–1.0) and `provider`
  (`deterministic` | `aws_bedrock`). Explicitly-stated values may set
  `confidence: 1.0`.
- Every model that makes a claim about content carries `supportingSlides` /
  `slideIds` / `sourceSlideIds` referencing manifest `unitId`s — **traceability
  is mandatory (PRD R4)**.
- IDs are stable, kebab/prefixed: `module-001`, `obj-004`, `assessment-002`,
  `reeng-007`. Slide references use the manifest's existing `unitId`.
- Nothing here replaces a manifest field. The course model *references* the
  manifest; the per-slide `pedagogical` block is purely additive.

---

## 1. Top-level — `course-model.json`

```jsonc
{
  "schemaVersion": "1.0",
  "artifactKind": "course_model",
  "documentId": "<from manifest>",
  "sourceManifestKey": "<path/key of the extraction manifest>",
  "createdAt": "<ISO-8601 UTC>",
  "course": {
    "metadata": { /* CourseMetadata */ },
    "modules": [ /* Module[] */ ],
    "slides": [ /* SlideInstructionalMetadata[] — one per manifest unit */ ],
    "objectives": [ /* LearningObjective[] */ ],
    "assessments": [ /* Assessment[] */ ],
    "pedagogicalAnalysis": {
      "objectiveAlignment": [ /* ObjectiveAlignment[] */ ],
      "bloomAnalysis": [ /* BloomAnalysis[] */ ],
      "moduleSequences": [ /* ModuleInstructionSequence[] */ ],
      "slideDensity": [ /* SlideDensityAnalysis[] */ ],
      "redundancy": [ /* RedundancyAnalysis[] */ ]
    },
    "reengineeringCandidates": [ /* ReengineeringCandidate[] */ ]
  },
  "providerUsage": { /* LLM token/cost usage, mirroring manifest.usage */ },
  "errors": [ { "stage": "...", "message": "...", "recoverable": true } ]
}
```

## 2. CourseMetadata

```jsonc
{
  "courseTitle": "string",
  "sourceFile": "string",
  "slideCount": 0,
  "estimatedModules": 0,
  "detectedAudience": "string|null",
  "detectedInstructionType": "Awareness|Procedural|Technical|Maintenance|Troubleshooting|Certification|Performance-based|Reference-only",
  "courseLevel": "string|null",
  "deliveryMode": "string|null",
  "containsAssessments": true,
  "containsLabs": false,
  "containsKnowledgeChecks": true,
  "containsInstructorLedPatterns": true,
  "containsPassiveLectureBias": false,
  "confidence": 0.0,
  "provider": "deterministic|aws_bedrock"
}
```

## 3. Module

Modules are **inferred** from slide sequence using: section-divider slides,
title changes, topic shifts, layout changes, speaker-note transitions,
repeated terminology clusters, agenda patterns. **Modules preserve original
slide references — no slide is moved or renumbered (PRD R1/R2).**

```jsonc
{
  "id": "module-001",
  "title": "string",
  "startSlide": "<unitId>",
  "endSlide": "<unitId>",
  "summary": "string",
  "dominantTopic": "string",
  "learningObjectiveIds": ["obj-001"],
  "slideIds": ["<unitId>", "..."],
  "instructionType": "<same enum as CourseMetadata.detectedInstructionType>",
  "estimatedBloomRange": { "low": "remember", "high": "apply" },
  "containsAssessment": true,
  "containsPractice": false,
  "containsFeedback": false,
  "containsRetentionSupport": false,
  "confidence": 0.0,
  "provider": "deterministic|aws_bedrock"
}
```

## 4. LearningObjective

Extracted from explicit slide text, explicit speaker notes, and implicit
instructional content. If not explicitly stated → `type: "Inferred"`,
`confidence < 1.0`. Air Force objectives populate task/condition/standard.

```jsonc
{
  "id": "obj-001",
  "type": "Terminal|Enabling|Inferred",
  "statement": "string",
  "verb": "string",
  "task": "string|null",
  "condition": "string|null",
  "standard": "string|null",
  "bloomLevel": "remember|understand|apply|analyze|evaluate|create",
  "moduleId": "module-001",
  "supportingSlides": ["<unitId>", "..."],
  "assessmentEvidence": ["assessment-001"],
  "confidence": 0.0,
  "provider": "deterministic|aws_bedrock",
  "source": "slide_text|speaker_notes|inferred"
}
```

**AF example.** "Perform hydraulic pressure inspection IAW TO under field
maintenance conditions with zero safety violations" →
`task`: "Perform hydraulic pressure inspection IAW TO";
`condition`: "under field maintenance conditions";
`standard`: "zero safety violations".

## 5. SlideInstructionalMetadata

One per manifest `unit`. `role` is a **list** — a slide may carry several
roles. Allowed roles: `Objective, Agenda, Concept, Definition, Explanation,
Demonstration, Example, Procedure, Activity, Guided Practice, Independent
Practice, Assessment, Feedback, Recap, Transition, Reference, Redundant,
Administrative`.

```jsonc
{
  "slideId": "<unitId>",
  "role": ["Concept", "Definition"],
  "supportsObjective": ["obj-004"],
  "moduleId": "module-002",
  "bloomSignal": "remember|understand|apply|analyze|evaluate|create|null",
  "containsTaskInstruction": false,
  "containsPerformanceCriteria": false,
  "containsAssessment": false,
  "containsPractice": false,
  "containsFeedback": false,
  "containsRetrievalPrompt": false,
  "contentDensity": "low|medium|high",
  "passiveLearningRisk": "low|medium|high",
  "instructionalValue": "low|medium|high",
  "redundancyRisk": "low|medium|high",
  "confidence": 0.0,
  "provider": "deterministic|aws_bedrock"
}
```

This is the same data echoed into the manifest slide's additive `pedagogical`
block (PRD §6.2) — `course-model.json` is canonical; the manifest block is a
denormalized convenience copy.

## 6. ObjectiveAlignment

The Air Force alignment check (PRD R5) — does instruction support the task,
does assessment verify it.

```jsonc
{
  "objectiveId": "obj-001",
  "instructionSupportScore": 0.0,
  "assessmentSupportScore": 0.0,
  "taskCoverage": 0.0,
  "conditionCoverage": 0.0,
  "standardCoverage": 0.0,
  "performanceReadinessRisk": "low|medium|high",
  "findings": [
    { "issue": "string", "slideIds": ["<unitId>"], "severity": "low|medium|high" }
  ],
  "confidence": 0.0,
  "provider": "deterministic|aws_bedrock"
}
```

## 7. BloomAnalysis

Bloom classified from objective verb + instructional demand + assessment
demand. **Do not penalize low Bloom when the task only requires Apply (R6).**

```jsonc
{
  "objectiveId": "obj-001",
  "objectiveBloom": "apply",
  "instructionBloom": "understand",
  "assessmentBloom": "apply",
  "appropriatenessScore": 0.0,
  "mismatchType": "aligned|overBloomed|underBloomed|assessmentMismatch|instructionMismatch",
  "rationale": "string",
  "confidence": 0.0,
  "provider": "deterministic|aws_bedrock"
}
```

## 8. ModuleInstructionSequence (Gagné)

Each module mapped against Gagné's nine events: 1 gain attention, 2 inform
objectives, 3 recall prior learning, 4 present content, 5 guidance, 6 practice,
7 feedback, 8 assessment, 9 retention/transfer. Classify only — do not rewrite.

```jsonc
{
  "moduleId": "module-001",
  "gagneCoverage": {
    "gainAttention": { "present": true, "slideIds": ["<unitId>"] },
    "informObjectives": { "present": false, "slideIds": [] },
    "recallPriorLearning": { "present": false, "slideIds": [] },
    "presentContent": { "present": true, "slideIds": ["<unitId>"] },
    "guidance": { "present": false, "slideIds": [] },
    "practice": { "present": false, "slideIds": [] },
    "feedback": { "present": false, "slideIds": [] },
    "assessment": { "present": true, "slideIds": ["<unitId>"] },
    "retentionTransfer": { "present": false, "slideIds": [] }
  },
  "missingEvents": ["informObjectives", "practice", "feedback"],
  "weakEvents": ["recallPriorLearning"],
  "sequenceIntegrityScore": 0.0,
  "confidence": 0.0,
  "provider": "deterministic|aws_bedrock"
}
```

## 9. Assessment

```jsonc
{
  "id": "assessment-001",
  "type": "quiz|knowledgeCheck|scenarioQuestion|discussionPrompt|practicalCheck|endOfModuleTest|embeddedQuestion",
  "slides": ["<unitId>", "..."],
  "measuresObjective": ["obj-001"],
  "bloomLevel": "remember|understand|apply|analyze|evaluate|create",
  "criterionBased": false,
  "performanceBased": false,
  "assessmentRisk": "low|medium|high",
  "confidence": 0.0,
  "provider": "deterministic|aws_bedrock"
}
```

## 10. SlideDensityAnalysis

```jsonc
{
  "slideId": "<unitId>",
  "densityScore": 0.0,
  "overloadRisk": "low|medium|high",
  "rewriteCandidate": false,
  "metrics": {
    "textDensity": 0.0,
    "bulletDensity": 0.0,
    "conceptCount": 0,
    "visualTextImbalance": 0.0,
    "titleBodyMismatch": false,
    "multipleObjectives": false
  },
  "reasons": ["string"]
}
```

Density metrics are **deterministic** — computed from manifest geometry, block
counts, and text length. No LLM required.

## 11. RedundancyAnalysis

```jsonc
{
  "id": "redundancy-001",
  "slides": ["<unitId>", "<unitId>"],
  "reason": "duplicateConcept|repeatedDefinition|unnecessaryReinforcement|repeatedGraphic",
  "mergeCandidate": true,
  "removeCandidate": false,
  "confidence": 0.0,
  "provider": "deterministic|aws_bedrock"
}
```

## 12. ReengineeringCandidate

**Candidates only — no rewrites (PRD R2).**

```jsonc
{
  "id": "reeng-001",
  "slideIds": ["<unitId>", "..."],
  "type": "splitSlide|rewriteObjective|addPractice|addRecap|insertFeedback|addRetrievalPrompt|reorderSequence|replaceAssessment|removeRedundancy",
  "reason": "string",
  "pedagogicalIssue": "string",
  "priority": "low|medium|high",
  "confidence": 0.0,
  "provider": "deterministic|aws_bedrock"
}
```

## 13. `course-analysis-summary.json`

Lightweight roll-up for orchestration — no per-slide detail.

```jsonc
{
  "schemaVersion": "1.0",
  "artifactKind": "course_analysis_summary",
  "documentId": "string",
  "scores": {
    "instructionalReadiness": 0.0,
    "objectiveAlignment": 0.0,
    "sequenceIntegrity": 0.0,
    "assessmentCoverage": 0.0
  },
  "risks": {
    "passiveLectureBias": "low|medium|high",
    "overloadedSlides": 0,
    "redundantSlides": 0,
    "performanceReadinessRisk": "low|medium|high"
  },
  "rebuildPriority": "low|medium|high",
  "moduleSummaries": [
    { "moduleId": "module-001", "title": "string", "readiness": 0.0,
      "topIssues": ["string"] }
  ],
  "reengineeringCandidateCount": 0
}
```

## 14. `reengineering-input.json`

The minimal hand-off to the redesign agent — **only slides flagged for
review**.

```jsonc
{
  "schemaVersion": "1.0",
  "artifactKind": "reengineering_input",
  "documentId": "string",
  "sourceManifestKey": "string",
  "flaggedSlides": [
    {
      "slideId": "<unitId>",
      "moduleId": "module-002",
      "objectives": [ /* the LearningObjective[] this slide supports */ ],
      "issues": [ /* findings: density, redundancy, alignment, Bloom, Gagné */ ],
      "rationale": "string",
      "reengineeringCandidates": ["reeng-001"],
      "moduleContext": { "title": "string", "instructionType": "string" },
      "sequencingContext": { "missingEvents": ["practice"], "weakEvents": [] }
    }
  ]
}
```

## 15. JSON Schema

A strict Draft 2020-12 schema for `course-model.json` (and the two companion
artifacts) ships in the implementation, mirroring experiment6's
`docling_schema` approach: enum-checked roles/types/mismatch values,
`additionalProperties: false` at the top level, required `confidence` +
`provider` on every inferred object, and validated in tests.
