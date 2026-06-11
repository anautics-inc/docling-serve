AI Course Reengineering Engine – Metadata Capture and Course Preparation Extension

Purpose

Extend the current PPTX extraction pipeline so the output is no longer just rendering metadata, but a pedagogical course model that is ready for instructional analysis and later slide reengineering.

The current system should remain the source of truth for geometry, layout, text, assets, notes, and rendering. The next stage adds instructional metadata, objective modeling, sequencing analysis, and pedagogical preparation.

This phase is NOT rebuilding slides.

This phase is:

enrich
classify
organize
normalize
prepare for instructional AI analysis

The output of this stage becomes the structured course model passed to a pedagogical reengineering agent.

Current Source Artifacts (already exist)

System currently produces:

pptx-ooxml-geometry.json
canvas-contract.json
pptx-ooxml-geometry.tldr
preview.html
extracted assets/*
indexed xml/*

Current extraction capabilities preserved

Do NOT break or remove existing capabilities.

Current preserved capabilities:

PPTX direct OOXML parsing
slide geometry preservation
coordinates (EMU, inch, px)
text runs
font metadata
color metadata
ordered line breaks
title/subtitle preservation
master/header image extraction
content image vision analysis
speaker notes extraction
image context extraction
Bloom scaffold placeholder
slide format metadata
preview rendering

New Goal

Take current extraction output and extend it into a pedagogical course model.

This creates the metadata layer needed before AI slide reengineering.

New Processing Stage

Course Preparation / Pedagogical Metadata Layer

Input

pptx-ooxml-geometry.json

Agent should use this as source of truth.

Do not re-parse PPTX in this phase.

Responsibilities

Course structure inference
instructional classification
learning objective extraction
Air Force performance modeling
pedagogical sequencing preparation
assessment detection
redundancy detection
instructional readiness scoring
package output for reengineering agent

Core Extension Model

Create new artifact:

course-model.json

This becomes canonical pedagogical representation.

Top-level structure

Course
metadata
modules[]
slides[]
objectives[]
assessments[]
pedagogicalAnalysis
reengineeringCandidates

Course Metadata Layer

Extend current file metadata with:

CourseMetadata
courseTitle
sourceFile
slideCount
estimatedModules
detectedAudience
detectedInstructionType
courseLevel
deliveryMode
containsAssessments
containsLabs
containsKnowledgeChecks
containsInstructorLedPatterns
containsPassiveLectureBias

Instruction type examples:

Awareness
Procedural
Technical
Maintenance
Troubleshooting
Certification
Performance-based
Reference-only
Module Inference Layer

Agent must infer modules from slide sequence.

Detection signals:

section divider slides
title changes
topic shifts
layout changes
speaker note transitions
repeated terminology clusters
agenda patterns

Output model

Module
id
title
startSlide
endSlide
summary
dominantTopic
learningObjectives[]
slides[]
instructionType
estimatedBloomRange
containsAssessment
containsPractice
containsFeedback
containsRetentionSupport

Rule:
Modules must preserve original slide references.

Do not move slides in this phase.

Objective Extraction Layer

Current system has deterministic Bloom scaffold placeholder.

Replace with inferred objective model.

Extract:

explicit objectives from slides
explicit objectives from notes
implicit objectives from instructional content

Output model

LearningObjective
id
type (Terminal | Enabling | Inferred)
statement
verb
task
condition
standard
bloomLevel
moduleId
supportingSlides[]
assessmentEvidence[]
confidence

Rules

If objective not explicitly stated:
infer but mark as inferred

If Air Force style objective found:
extract task / condition / standard

Example:

Perform hydraulic pressure inspection IAW TO under field maintenance conditions with zero safety violations

Should produce:

task
condition
standard

Slide Instructional Classification Layer

Each slide should be classified.

Allowed slide roles:

Objective
Agenda
Concept
Definition
Explanation
Demonstration
Example
Procedure
Activity
Guided Practice
Independent Practice
Assessment
Feedback
Recap
Transition
Reference
Redundant
Administrative

Each slide gets:

SlideInstructionalMetadata
role[]
supportsObjective[]
moduleId
bloomSignal
containsTaskInstruction
containsPerformanceCriteria
containsAssessment
containsPractice
containsFeedback
containsRetrievalPrompt
contentDensity
passiveLearningRisk
instructionalValue
redundancyRisk

Air Force Objective Alignment Layer

This is critical.

Do NOT treat Bloom as primary.

Evaluate each module and slide against:

Task
Condition
Standard

Questions:

What must learner do?
Under what conditions?
To what standard?
Does instruction support this?
Does assessment verify this?

Output:

ObjectiveAlignment
objectiveId
instructionSupportScore
assessmentSupportScore
taskCoverage
conditionCoverage
standardCoverage
performanceReadinessRisk
findings[]

Bloom Appropriateness Layer

Current deterministic Bloom scaffold is not enough.

Agent should classify Bloom using:

objective verb
instructional demand
assessment demand

Rules:

Do NOT penalize low Bloom if procedural training requires Apply only.

Flag only when Bloom is wrong for task.

Outputs:

BloomAnalysis
objectiveBloom
instructionBloom
assessmentBloom
appropriatenessScore
mismatchType

Possible mismatch types:

overBloomed
underBloomed
assessmentMismatch
instructionMismatch
aligned
Gagne Sequencing Preparation Layer

Map module flow against:

1 Gain attention
2 Inform objectives
3 Recall prior learning
4 Present content
5 Guidance
6 Practice
7 Feedback
8 Assessment
9 Retention / transfer

Output:

ModuleInstructionSequence
moduleId
gagneCoverage
missingEvents[]
weakEvents[]
sequenceIntegrityScore

Do not rewrite slides yet.

Only classify sequence.

Assessment Detection Layer

Detect:

quizzes
knowledge checks
scenario questions
discussion prompts
practical checks
end-of-module tests
embedded questions

Output:

Assessment
id
type
slides[]
measuresObjective[]
BloomLevel
criterionBased
performanceBased
assessmentRisk

Content Density / Overload Detection

For each slide:

Measure:

text density
bullet density
concept count
visual/text imbalance
instructional overload risk
title/body mismatch
multiple objectives in one slide

Output:

SlideDensityAnalysis
slideId
densityScore
overloadRisk
rewriteCandidate
reasons[]

Redundancy Detection

Identify:

duplicate concept slides
repeated definitions
unnecessary reinforcement
repeated graphics without instructional purpose

Output:

RedundancyAnalysis
slides[]
reason
mergeCandidate
removeCandidate

Reengineering Candidate Detection

Do NOT rebuild.

Identify candidates only.

Categories:

split slide
rewrite objective
add practice
add recap
insert feedback
add retrieval prompt
reorder sequence
replace assessment
remove redundancy

Output:

ReengineeringCandidate
id
slideIds[]
type
reason
pedagogicalIssue
priority
confidence

New Output Files

course-model.json

Canonical pedagogical course model

Contains:

course metadata
modules
objectives
slide instructional metadata
assessments
sequencing analysis
overload analysis
reengineering candidates
course-analysis-summary.json

Lightweight summary for downstream orchestration

Contains:

pedagogical scores
risks
readiness
rebuild priority
module summaries
reengineering-input.json

Minimal artifact sent to slide redesign agent

Contains:

only slides flagged for review
objectives
issues
rationale
module context
sequencing context

Current JSON Extension Requirements

Extend existing slide object.

Do NOT replace current geometry model.

Add:

pedagogical

Example:

slide:
existingGeometry...
existingText...
existingAssets...
existingFormat...

pedagogical:
role[]
moduleId
inferredObjectiveIds[]
bloomSignal
densityScore
overloadRisk
assessmentSignal
retrievalSignal
feedbackSignal
instructionalValue
redundancyRisk
reengineeringCandidate

Agent Rules

Rule 1
Geometry extraction remains source of truth

Never mutate layout coordinates in metadata stage

Rule 2
Do not rewrite slide content in this phase

Only classify and prepare

Rule 3
Pedagogical metadata must be deterministic where possible

Use confidence scoring where inference is used

Rule 4
Preserve source traceability

Every inference must map back to source slide IDs

Rule 5
Air Force instructional model overrides generic academic Bloom scoring

Primary question:

Can learner perform required task under defined conditions to standard?

Rule 6
Do not assume higher Bloom is better

Procedural courses may correctly stay at Apply

Rule 7
Use speaker notes heavily

Speaker notes often contain:

instructor intent
hidden objectives
practice guidance
assessment context

Implementation Sequence for Agent

Step 1
Load pptx-ooxml-geometry.json

Step 2
Infer course metadata

Step 3
Infer modules

Step 4
Extract objectives

Step 5
Classify slides

Step 6
Assess objective alignment

Step 7
Assess Bloom appropriateness

Step 8
Map Gagne sequence

Step 9
Detect assessments

Step 10
Detect density and redundancy

Step 11
Generate reengineering candidates

Step 12
Emit:

course-model.json
course-analysis-summary.json
reengineering-input.json

Success Criteria

no existing extraction data lost
every slide classified
modules inferred
objectives inferred
Air Force task-condition-standard modeled
assessment alignment modeled
Bloom appropriateness modeled
Gagne coverage modeled
reengineering candidates identified
downstream redesign agent receives structured artifact only for slides needing redesign

Final Principle

This stage prepares the course for AI instructional redesign.

It does not redesign the course.

It organizes instructional truth so a downstream pedagogical agent can intelligently decide what should change and why.