# Experiment 5 Findings: Placeholder and Master Typography Inheritance

**Run date:** 2026-05-20  
**Commands:**

```bash
UV_CACHE_DIR=/tmp/uv-cache uv run python tests/experiment5/run_experiment.py
UV_CACHE_DIR=/tmp/uv-cache uv run pytest tests/experiment5/tests/
```

## Result

- 7/7 PPTX fixtures completed with `status=complete`.
- Pytest: 74 passed.
- Generated manifests validate against the experiment5 schema.
- `tests/experiment5/out/_summary/presentation_coverage.csv` was generated from the experiment5 manifests.
- `themed_colors_resolved` is greater than zero on every fixture; corpus total is 3,742 resolved inherited/theme-backed colors.

## Before/After Coverage

| Fixture | v4 size | v5 size | v5 color | themed_colors_resolved | dominant size source |
| --- | ---: | ---: | ---: | ---: | --- |
| `2024 D200F Applications, Programs and Indentures.pptx` | 96.16% | 99.76% | 99.76% | 1,426 | `run` |
| `SPM-Welcome-Page-Highlights.pptx` | 77.78% | 100.00% | 100.00% | 18 | `run` |
| `ESCAPE Centralization Slide Deck v5.pptx` | 21.23% | 100.00% | 100.00% | 171 | `masterTextStyle` |
| `TCTO_Slides_2026.pptx` | 7.06% | 99.62% | 99.62% | 1,789 | `masterTextStyle` |
| `6. 2025 Code_Validation.pptx` | 77.40% | 97.95% | 97.95% | 123 | `run` |
| `updated AFTO Form 874 for presentation.pptx` | 77.33% | 82.67% | 82.67% | 160 | `run` |
| `Interchangeability and Substitutability (I&S) & OOU (Order of Use).pptx` | 60.71% | 100.00% | 100.00% | 55 | `run` |

## Findings

- Experiment5 proves the inheritance chain works. The two fixtures that drove this experiment, `TCTO_Slides_2026` and `ESCAPE Centralization`, moved from 7.06% and 21.23% size coverage to 99.62% and 100.00%.
- The remaining raw misses are newline-only runs with empty `resolvedFrom` maps, not visible content. When empty/whitespace-only runs are excluded, every fixture has 100.00% content-bearing `runs_with_size`.
- `updated AFTO Form 874 for presentation.pptx` is the only fixture below the strict raw 90% threshold at 82.67%. All 39 missing size/color runs are `"\n"` runs, mostly inside title placeholders, so this does not currently indicate a second slide master issue.
- The corpus contains `<a:schemeClr>` references in presentation, master, and slide XML. Experiment5 now resolves inherited colors to concrete hex values through the theme; `themed_colors_resolved` is non-zero across all fixtures.
- Every slide still uses inherited backgrounds from layout/master in this corpus. No fixture exercised explicit slide backgrounds or image backgrounds.

## Experiment6 Triggers

- Keep second-slide-master support on the experiment6 watch list, but this run did not expose a content-bearing fixture below 90% size coverage.
- Add normalization for newline-only runs so non-visible text does not depress raw typography coverage.
- Group recursion remains open.
- Table-cell extraction remains open.
- Color provenance should be made explicit in the manifest, e.g. preserve whether a resolved hex came from `srgbClr`, `sysClr`, or `schemeClr`, instead of inferring from `resolvedFrom.color`.
