# Experiment 3 Findings: Tightened Deep Document Contract

**Run date:** 2026-05-20  
**Commands:**

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python tests/experiment3/run_experiment.py
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/experiment3/tests/
```

## Result

Experiment3 completed the PR1/PR2/PR3 tightening pass:

- 7/7 fixtures completed with `status=complete`.
- 7/7 generated manifests validated against the strict Draft 2020-12 schema.
- Pytest: 36 passed, 0 skipped after corpus generation.
- No `slideImageRef` or `tldrawCommands` fields are present in outputs.
- Decorative placeholders (`sldNum`, `dt`, `ftr`) are no longer emitted as blocks.
- Empty notes now use `speakerNotes.classification = null`.
- Opaque no-text blocks now use `method="opaque_structural_no_content"` and `confidence=0.0`.

## Experiment2 vs Experiment3

| Fixture | Blocks v2 | Blocks v3 | Title Roles v2 | Title Roles v3 | Fallback v2 | Fallback v3 | Dominant Role v2 | Dominant Role v3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| `1220dd73-...-updated AFTO Form 874 for presentation.pptx` | 94 | 67 | 29 | 27 | 0.8404 | 0.7761 | concept_explanation | title |
| `1d7087c1-...-SPM-Welcome-Page-Highlights.pptx` | 25 | 22 | 5 | 3 | 1.0000 | 1.0000 | summary_or_closing | concept_explanation |
| `7866da0f-...-2024 D200F Applications, Programs and Indentures.pptx` | 1,251 | 1,251 | 208 | 208 | 0.9552 | 0.9552 | concept_explanation | concept_explanation |
| `Interchangeability and Substitutability (I&S) & OOU (Order of Use).pptx` | 31 | 31 | 10 | 9 | 0.7097 | 0.7097 | concept_explanation | concept_explanation |
| `bf4f0e25-...-TCTO_Slides_2026.pptx` | 752 | 507 | 252 | 249 | 0.8883 | 0.8343 | concept_explanation | title |
| `d540acc3-...-6. 2025 Code_Validation.pptx` | 160 | 104 | 20 | 17 | 0.9313 | 0.8942 | concept_explanation | concept_explanation |
| `f69af18d-...-ESCAPE Centralization Slide Deck v5.pptx` | 116 | 84 | 33 | 31 | 0.9569 | 0.9405 | concept_explanation | title |

## Spot Check

AFTO slide 1 previously let the slide-number placeholder pollute block/role counts. In experiment3:

- `decorations.slideNumberText = "1"` preserves the recovered text for audit.
- No emitted block has `placeholderType="sldNum"`.
- Slide 1 blocks are only the title, subtitle/example text, and `AFTO FORM 874`.
- The slide-level role is `title`, and block-level roles are derived from each block rather than the slide index.

## Notes

- D200F did not change block count because its OOXML did not expose the same decorative placeholder pattern as the other decks.
- Some dominant roles changed to `title` because title placeholders are now counted correctly without footer artifacts inflating the denominator.
- Fallback fraction only drops where decorative/phantom blocks were removed. It remains high on decks dominated by short labels or opaque screenshots; that is expected until Bedrock/VLM semantic enrichment is enabled for the full corpus.

