# Experiment 3: Tighten the Deep Document Contract

**Date:** 2026-05-20
**Parent:** `experiment2.md` and `experiment2-findings.md` in this folder.
**Status:** scaffolded with fixes applied; agent must run, regression-test, and iterate.

---

## What this experiment is

Experiment3 is **not** a new pipeline. It is a tightening pass that takes experiment2's working code and ships three coherent fix bundles (PR1, PR2, PR3) that close the six defects flagged in the experiment2 audit. The architecture, provider abstraction, S3-shaped paths, manifest schema 2.0, and Bedrock hook stay exactly as experiment2 defined them.

When experiment3 is done the manifest produced for any fixture should be (a) structurally indistinguishable from experiment2's *except for the defects below*, (b) validate against a stricter on-disk JSON Schema, and (c) survive a pytest regression suite.

## What's already done in this scaffold

The `tests/experiment3/` folder ships with experiment2's code copied over and the PR1+PR2+m3 fixes already applied inline. The agent's job is to:

1. Run the experiment end-to-end against all 7 fixtures.
2. Verify the regression tests in `tests/experiment3/tests/` pass.
3. Compare `tests/experiment3/out/_summary/comparison.csv` against experiment2's CSV and document deltas in `experiment3-findings.md`.
4. Tighten anything still wrong.

If any of the fixes below are wrong in spirit, the agent has the audit context to push back instead of rubber-stamping.

---

## PR1 — Per-block role + footer placeholder filter (M1, M2)

### Symptoms in experiment2

- AFTO slide-001 has `sldNum` block (`text="1"`) labeled `role=title` because `classify_role` short-circuits to `"title"` when `index==0`.
- Last slide's `sldNum` ("27") and empty `kind=other` block are both labeled `role=summary_or_closing` because of the parallel `index==total-1` clause.
- `roleDistribution.title = 29` for a 27-slide deck — inflated by footer artifacts.
- Block counts are inflated by ~1/slide because every slide has a `sldNum` placeholder; TCTO_Slides_2026 has 250 slides → 250 phantom blocks.

### Fix shape

**`deep_document/bloom_classifier.py`**

- Drop `index`/`total` parameters from `classify_role` and `classify`. Per-block classification must not inherit slide-positional rules.
- Add a sibling helper:
  ```python
  def slide_position_role(*, index: int, total: int) -> str | None:
      if index == 0:
          return "title"
      if total and index == total - 1:
          return "summary_or_closing"
      return None
  ```
- `aggregate_classification` keeps `index`/`total` parameters and uses `slide_position_role` only as a tiebreaker when the aggregated `dominant_role` is `concept_explanation` or `reference`. A slide with a strong content signal (e.g. a `learning_objective`-pattern hit) wins over its positional rule.

**`deep_document/ooxml.py`**

- Add module constant `DECORATIVE_PLACEHOLDER_TYPES = {"sldNum", "dt", "ftr"}`.
- In `parse_pptx`, skip shapes whose placeholder type is in that set. Stash any recovered text on the unit as:
  ```python
  unit["decorations"] = {
      "slideNumberText": "...",   # the literal "1", "27", etc.
      "footerText": "...",         # may carry "Confidential" or similar
      "dateText": "...",
  }
  ```
  This keeps the content recoverable for audit without polluting `blocks` or the Bloom totals.

**`deep_document/semantics.py`**

- `unit_context`'s `total` arg stays. `block_context` no longer receives `index`/`total`.
- `aggregate_classification` is now the only call path that knows about slide position.

### Acceptance

- No block in any manifest has `placeholderType` in `{"sldNum","dt","ftr"}`.
- `deckSummary.roleDistribution["title"]` ≤ slide count for every fixture.
- A slide whose content has a `learning_objective` pattern match keeps `role="learning_objective"` even on the last slide.
- A new pytest regression `tests/test_role_classification.py` covers both rules.

---

## PR2 — Opaque blocks, empty notes, asset evidence (M3, M4, m1, m3)

### Symptoms in experiment2

- `chart_placeholder`, `smartart_placeholder`, `group`, and `kind=other` blocks with no text get `level=understand@0.28` from the verb-matcher fallback, indistinguishable from real text content.
- Block context falls through `block.get("text") or block.get("shapeName") or block.get("assetId") or block["kind"]`, feeding shape names like `"TextBox 4"` into the verb matcher.
- Every slide's `speakerNotes.classification` is populated even when `cleaned is None`, returning a synthetic `remember@0.18` against `text=""`.
- Picture-asset evidence excerpt is the joined relationship-path string (e.g. `"ppt/media/image1.png ppt/media/image2.png"`).
- Dead self-assignment loop in `apply_semantic_annotations` at semantics.py:411-419.

### Fix shape

**`deep_document/bloom_classifier.py`**

- Add `OPAQUE_BLOCK_KINDS = {"chart_placeholder", "smartart_placeholder", "group", "other"}`.
- Add `classify_opaque_structural(kind: str)` returning:
  ```python
  {
    "level": "understand",
    "role": "reference",
    "confidence": 0.0,                       # honest: we know nothing
    "provider": "deterministic_fallback",
    "method": "opaque_structural_no_content",
    "evidence": [{
        "source": "block_text", "excerpt": "", "terms": [],
        "reason": f"{kind} block has no readable text; needs VLM caption or human review."
    }],
    "recommendedImprovements": [],
  }
  ```
- Rewrite `classify_picture_asset` to drop the joined-path "text" and emit `excerpt: ""` with a real reason.

**`deep_document/semantics.py`**

- `DeterministicFallbackProvider.classify` branches:
  ```python
  if context.target_type == "asset":
      return bloom.classify_picture_asset(...)
  if context.kind == "table":
      return bloom.classify_table(context.text)
  if (not context.text.strip()) and context.kind in bloom.OPAQUE_BLOCK_KINDS:
      return bloom.classify_opaque_structural(context.kind)
  return bloom.classify(context.text, ...)
  ```
- `BedrockSemanticProvider` honours the same opaque-block branch in `_fallback_with_reason` (and never sends opaque-empty contexts to Bedrock — they're a deterministic waste of model cost).
- The block context's `text` is `block.get("text") or ""` (no fallback to shapeName/assetId/kind). Structural signals carry shapeName/assetId/relationshipId so the provider still sees them.
- `apply_semantic_annotations` skips notes whose `cleaned` is falsy:
  ```python
  notes = unit["speakerNotes"]
  if notes.get("cleaned"):
      contexts.append(notes_context)
      notes_contexts[notes_context.target_id] = (notes, notes_context)
  else:
      notes["classification"] = None
      notes["semanticAnnotationId"] = None
  ```
- Delete the dead self-assignment loop (m3).
- `aggregate_classification` filters out `None` from the classifications list.

**`deep_document/schema.py`**

- `check_classification` accepts `None` when the parent path is `*.speakerNotes` and `cleaned` is `None`. Implementation: pass an `allow_none` flag when walking the notes object.

### Acceptance

- No picture-asset evidence excerpt contains the literal string `"ppt/media/"`.
- No `chart_placeholder`/`smartart_placeholder`/`group`/`other` block with empty text has `method == "default_no_direct_bloom_verb"`. They all carry `method == "opaque_structural_no_content"` and `confidence == 0.0`.
- For at least one fixture, some slide has `speakerNotes.cleaned == null` AND `speakerNotes.classification == null`.
- `validate_manifest` accepts that null pattern; rejects null in any other classification slot.
- Two new pytest regressions in `tests/test_opaque_blocks.py` and `tests/test_empty_notes_null.py`.

---

## PR3 — Real JSON Schema + pytest suite (M5, M6)

### Symptoms in experiment2

- On-disk `schema/deep-document-manifest.schema.json` is a stub with `additionalProperties: True`, no enums, no item shapes. A downstream consumer cannot validate the manifest with `jsonschema.validate` against this schema and trust the result.
- `tests/experiment2/tests/` directory was never created. No regression suite.

### Fix shape

**`deep_document/schema.py`**

- Replace the hand-rolled `validate_manifest` with a real Draft 2020-12 JSON Schema. Required elements:
  - Top-level `additionalProperties: False`. Every field in the manifest is enumerated.
  - `extraction.mode` is `const: "deep"`.
  - `level` is `enum: BLOOM_LEVELS`. `role` is `enum: INSTRUCTIONAL_ROLES`. `provider` is `enum: {"deterministic_fallback","aws_bedrock_structured_output"}`.
  - `units[].blocks[]` has a strict item shape with `oneOf` discrimination by `kind`.
  - `units[].speakerNotes.classification` is `type: ["object","null"]` and uses an `if/then` rule keyed on `cleaned` to enforce the null pattern.
  - `assets[].classification` is required and must carry `method: "no_caption_available"` until a VLM provider is introduced.
- Keep `FORBIDDEN_KEYS` walk as a belt-and-suspenders check (catches accidental `slideImageRef`/`tldrawCommands` leaks even if a future schema change widens `additionalProperties`).
- `validate_manifest(manifest)` returns a list of `(path, message)` tuples by calling `jsonschema.Draft202012Validator(schema).iter_errors(manifest)`. Add `jsonschema` to project dev deps if not already present.

**`tests/experiment3/schema/deep-document-manifest.schema.json`**

- Written by `schema.write_schema()` and checked in. CI must fail if the on-disk schema diverges from the in-code definition.

**`tests/experiment3/tests/`**

Required files (the scaffold ships stubs; agent fills them in):

- `test_ids_are_stable.py` — `extraction_id(sha, opts)` is byte-equal for the same `(sha, opts)`. Different opts → different id.
- `test_notes_cleaner.py` — `clean_notes("3", 3)` returns `(None, True)`. Slide-3 of the AFTO manifest, after a re-run, has `speakerNotes.cleaned is None`.
- `test_bloom_aggregation.py` — `aggregate_classification([analyze, understand, remember], "...")` returns `level="analyze"`. Empty list returns deterministic fallback.
- `test_role_classification.py` — PR1 regressions: `sldNum` block never reaches `classify_role`; `slide_position_role(index=0, total=27)=="title"`; a strong content match beats positional inference in `aggregate_classification`.
- `test_opaque_blocks.py` — PR2 regression: a `chart_placeholder` block with empty text classifies with `method="opaque_structural_no_content"` and `confidence==0.0`.
- `test_empty_notes_null.py` — PR2 regression: a unit whose raw notes is `"\n"` has `speakerNotes.cleaned is None` and `speakerNotes.classification is None`; `validate_manifest` accepts that manifest.
- `test_manifest_schema_validates.py` — every manifest under `tests/experiment3/out/**/manifest.json` passes `jsonschema.validate(manifest, schema)`. Use `pytest.importorskip("jsonschema")`.
- `test_fixture_smoke.py` — discovers fixtures, runs `build_manifest_for_fixture` against the smallest 2 fixtures (skip large fixtures by default; gate large-fixture coverage on `EXPERIMENT3_FULL=1`).

The smoke test must use the smallest two fixtures because pytest needs to stay under ~30s in CI. The full corpus run lives in `run_experiment.py`.

### Acceptance

- `uv run pytest tests/experiment3/` passes locally with all 7+ tests green.
- `uv run python tests/experiment3/run_experiment.py` produces 7 manifests, all validating against the new strict schema.
- The on-disk schema file's sha256 matches `schema.MANIFEST_SCHEMA` serialized — write a tiny check in `test_manifest_schema_validates.py` to enforce this.

---

## What's intentionally not in experiment3

- m4 (timestamp idempotence): the spec contract permits `createdAt` to vary. Other timestamps are documented in experiment2-findings and would need a spec amendment to harden — call it out, do not silently move timestamps.
- m5 (validation pre-write): the current after-write flow with `status="failed"` is acceptable for an experiment surface. Production should split the artifact write from validation.
- Bedrock VLM picture captioning: still deferred. The opaque-block fix above is the polite stand-in until VLM lands.
- Canvas viewer / tldraw projection: still out of scope. Experiment4 territory.

---

## File map

```
tests/experiment3/
├── README.md
├── .gitignore                                  # out/
├── run_experiment.py                           # copied from experiment2; OUT_ROOT redirected
├── deep_document/
│   ├── __init__.py
│   ├── bloom_classifier.py                     # PR1 + PR2 applied
│   ├── ooxml.py                                # PR1 applied (placeholder filter)
│   ├── semantics.py                            # PR1 + PR2 + m3 applied
│   ├── manifest_builder.py                     # unchanged except for new helper signatures
│   └── schema.py                               # PR3 applied (real JSON Schema)
├── schema/
│   └── deep-document-manifest.schema.json      # generated by schema.write_schema
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_ids_are_stable.py
    ├── test_notes_cleaner.py
    ├── test_bloom_aggregation.py
    ├── test_role_classification.py
    ├── test_opaque_blocks.py
    ├── test_empty_notes_null.py
    ├── test_manifest_schema_validates.py
    └── test_fixture_smoke.py
```

---

## Definition of Done

- All 7 fixtures complete with `status=complete`.
- `tests/experiment3/out/_summary/comparison.csv` shows `roleDistribution.title` ≤ slide count for every fixture.
- `fallbackBlockFraction` drops measurably vs experiment2 once footer/decorations are excluded (they were inflating the denominator with synthetic understand@0.28).
- Pytest suite green.
- `experiment3-findings.md` lives alongside `experiment2-findings.md` with a side-by-side comparison table (slides, blocks, fallback fraction, dominant role) showing the delta.
