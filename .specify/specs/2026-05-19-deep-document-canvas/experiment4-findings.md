# Experiment 4 Findings: Typography, Theme, and Slide Background

**Run date:** 2026-05-20  
**Commands:**

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python tests/experiment4/run_experiment.py
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/experiment4/tests/
UV_CACHE_DIR=/tmp/uv-cache uv run python tests/experiment4/preview.py \
  --manifest tests/experiment4/out/_tenant=local/spaces/dataset=experiment4/b156f046844786eb-1220dd73-5621-458d-950e-657a6738fb14-updated\ AFTO\ Form\ 874\ for\ presentation.pptx/extractions/d56e9f7535be39a5/manifest.json \
  --out tests/experiment4/out/_summary/preview-af.to-slide1.html \
  --slide-index 0
```

## Result

Experiment4 completed the presentation-data pass:

- 7/7 fixtures completed with `status=complete`.
- Pytest: 53 passed.
- Generated manifests validate against the experiment4 schema.
- `manifest.theme` is emitted.
- Text blocks carry `paragraphs[].runs[]` with run-level typography when locally declared in slide XML.
- Every slide carries `background`, resolved through slide → layout → master.
- A tiny HTML preview was generated at `tests/experiment4/out/_summary/preview-af.to-slide1.html`.

## Presentation Coverage

| Fixture | Slides | Runs | Themed Colors Resolved | Runs With Size | Runs With Color | Explicit Background | Inherited Background | Image Background |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `7866da0f-...-2024 D200F Applications, Programs and Indentures.pptx` | 223 | 1,666 | 0 | 96.16% | 14.17% | 0 | 223 | 0 |
| `1d7087c1-...-SPM-Welcome-Page-Highlights.pptx` | 3 | 18 | 0 | 77.78% | 0.00% | 0 | 3 | 0 |
| `f69af18d-...-ESCAPE Centralization Slide Deck v5.pptx` | 33 | 179 | 0 | 21.23% | 4.47% | 0 | 33 | 0 |
| `bf4f0e25-...-TCTO_Slides_2026.pptx` | 250 | 1,826 | 0 | 7.06% | 1.64% | 0 | 250 | 0 |
| `d540acc3-...-6. 2025 Code_Validation.pptx` | 28 | 146 | 0 | 77.40% | 13.70% | 0 | 28 | 0 |
| `1220dd73-...-updated AFTO Form 874 for presentation.pptx` | 27 | 225 | 0 | 77.33% | 11.56% | 0 | 27 | 0 |
| `Interchangeability and Substitutability (I&S) & OOU (Order of Use).pptx` | 10 | 56 | 0 | 60.71% | 1.79% | 0 | 10 | 0 |

The generated machine-readable coverage CSV is `tests/experiment4/out/_summary/presentation_coverage.csv`.

## Findings

- `runs_with_size` is above 30% on five of seven fixtures, but low on `TCTO_Slides_2026` at 7.06% and `ESCAPE Centralization` at 21.23%.
- Experiment5 should prioritize layout/master placeholder typography inheritance. The missing font sizes are likely defined in layout/master text styles rather than direct slide-level run or paragraph properties.
- `runs_with_color` is sparse across the corpus. This is expected for decks that rely on inherited theme colors or default text color rather than explicit run color.
- `themed_colors_resolved` is 0 in this corpus because no parsed run used a direct `schemeClr` in the slide-level run properties walked by experiment4. Theme parsing itself is present and covered by tests.
- Every background was inherited from layout/master; no slide had an explicit slide-level background.
- No image backgrounds appeared in these seven fixtures. The schema and extraction code support image backgrounds, but this corpus did not exercise that path.

## Preview

`tests/experiment4/preview.py` renders a single slide to static HTML using:

- `unit.slideSizeEmu`
- `unit.background`
- `block.bbox`
- `block.paragraphs[].runs[]`
- picture `assetId` references

This is not a PowerPoint renderer. It is a contract proof that a web viewer can consume the experiment4 manifest directly.

