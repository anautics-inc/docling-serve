# Experiment 2 Findings: Docling + OOXML Deep PPTX Contract

**Run date:** 2026-05-20  
**Command:** `uv run python tests/experiment2/run_experiment.py`  
**Output summary:** `tests/experiment2/out/_summary/comparison.csv`

## Result

Experiment2 now produces one `manifest.json` per PPTX fixture using Docling plus OOXML structural parsing only. All 7 fixtures completed with `status=complete`, zero manifest validation errors, no `slideImageRef`, and no `tldrawCommands`.

The manifest now separates extractor-owned structure from semantic decisions:

- `units`, `blocks`, and `assets` are neutral structure/content records. They do not assume fixed titles, logos, deck templates, or known layouts.
- `semantics.annotations` is the source of truth for Bloom/role decisions, keyed by target IDs.
- `classification` fields remain denormalized onto slides, blocks, notes, and assets so simple consumers do not need to join.
- `CAPTIFY_DEEP_DOC_SEMANTIC_PROVIDER=bedrock` enables the GenAI decision provider shape; local A/B runs default to deterministic fallback.
- The selected semantic provider and selected Bedrock model are part of `effective_options.json` and the deterministic `extractionId`, so fallback and GenAI runs cannot collide under the same extraction key.
- In this AWS account, Claude Sonnet 4.5 must be invoked through an inference profile. The production default is `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, not the foundation model ID.

Totals from the final run:

- Fixtures: 7
- Slides: 574
- Blocks: 2,429
- OOXML media assets: 154
- Docling picture items: 165
- Generated manifests: 7
- Semantic annotations: one per block, slide, notes object, and asset; all manifests matched expected annotation coverage.
- Bedrock smoke extraction: `1d7087c1-e49f-43e8-b383-7992e0bf8edb-SPM-Welcome-Page-Highlights.pptx` completed with `DOCLING_SERVE_DEEP_DOC_SEMANTIC_PROVIDER=bedrock`, `fail_open=false`, zero errors, and provider coverage `aws_bedrock_structured_output=0.9`, `deterministic_fallback=0.1` for empty targets.

## Comparison Table

| Fixture | Slides | Blocks | OOXML Media | Docling Pictures | Raw Notes | Cleaned Notes | Dominant Bloom | Higher-Order Slides | Total Seconds | Errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| `1220dd73-...-updated AFTO Form 874 for presentation.pptx` | 27 | 94 | 11 | 9 | 4 | 1 | understand | 3 | 6.923 | 0 |
| `1d7087c1-...-SPM-Welcome-Page-Highlights.pptx` | 3 | 25 | 2 | 3 | 3 | 0 | understand | 0 | 9.271 | 0 |
| `7866da0f-...-2024 D200F Applications, Programs and Indentures.pptx` | 223 | 1,251 | 110 | 121 | 33 | 12 | understand | 11 | 22.573 | 0 |
| `Interchangeability and Substitutability (I&S) & OOU (Order of Use).pptx` | 10 | 31 | 1 | 4 | 0 | 0 | understand | 0 | 7.228 | 0 |
| `bf4f0e25-...-TCTO_Slides_2026.pptx` | 250 | 752 | 1 | 1 | 23 | 15 | understand | 12 | 9.469 | 0 |
| `d540acc3-...-6. 2025 Code_Validation.pptx` | 28 | 160 | 13 | 11 | 28 | 28 | understand | 10 | 10.066 | 0 |
| `f69af18d-...-ESCAPE Centralization Slide Deck v5.pptx` | 33 | 116 | 16 | 16 | 21 | 17 | understand | 6 | 10.188 | 0 |

## Open Questions Answered

1. **Does Docling export every embedded PPTX picture?**  
   No. Across the corpus, OOXML found 154 deduped media assets while Docling reported 165 picture items. The mismatch is not one-directional: AFTO and Code Validation had more OOXML media than Docling pictures, while D200F, SPM Welcome, and Interchangeability had more Docling picture items than OOXML media. This means production cannot treat Docling picture count as the asset source of truth; OOXML media must remain the asset table source, with Docling picture refs as provenance.

2. **Does `DoclingDocument.tables[].prov[].page_no` map to PPTX slide numbers?**  
   It mapped cleanly for this run: Docling page count equaled OOXML slide count for all 7 fixtures, and table refs were joinable by `prov[].page_no` to same-numbered slide units. Experiment2 now attaches page-level Docling provenance to each slide and assigns table/picture Docling refs to same-slide blocks where available. The next production improvement is a bbox/text-neighborhood join, because same-slide ordering is only a heuristic.

3. **What was the slowest fixture?**  
   `7866da0f-...-2024 D200F Applications, Programs and Indentures.pptx` was slowest at 22.573 seconds total. Docling dominates runtime in every fixture; OOXML parsing and deterministic Bloom classification are small by comparison.

4. **What happens for chart/SmartArt-heavy content?**  
   The Interchangeability fixture produced `smartart_placeholder` and `group` blocks, not editable chart data. Docling reported 4 picture items for that deck while OOXML exposed 1 media asset. This is a real gap: without a richer OOXML chart/SmartArt parser or Bedrock/VLM captioning, these blocks are structural placeholders with low-confidence taxonomy tags.

5. **How often do Docling and OOXML disagree on picture count?**  
   Five of seven fixtures disagreed. Media deltas (`ooxml_media - docling_pictures`) were: AFTO `+2`, SPM `-1`, D200F `-11`, Interchangeability `-3`, TCTO `0`, Code Validation `+2`, ESCAPE `0`.

6. **How much classification relies on fallback versus verb match?**  
   Fallback remains high, which is expected for slide decks with short labels and embedded screenshots. Block fallback fractions were: AFTO `0.8404`, SPM `1.0000`, D200F `0.9552`, Interchangeability `0.7097`, TCTO `0.8883`, Code Validation `0.9313`, ESCAPE `0.9569`. Production should swap in Bedrock structured taxonomy classification for text blocks and Bedrock/VLM captioning for picture assets, keeping deterministic fallback as the outage path.

## Sanity Checks

```text
fixtures_processed: 7
manifest_count: 7
untagged_blocks: 0 for every manifest
untagged_assets: 0 for every manifest
manifest_errors: 0 for every manifest
banned_fields: no "slideImageRef" or "tldrawCommands" under tests/experiment2/out
```

## Production Gaps

- The JSON Schema is intentionally lightweight structural validation. It catches missing top-level fields, missing classifications, and banned runtime fields, but should be hardened before serving as the API contract.
- Picture assets are deduped by OOXML binary hash, but Docling's generated image artifacts do not always hash-match the embedded binaries. Treat `doclingPictureId` as provenance, not as the canonical asset identifier.
- Table and picture joins are currently same-slide/order-based. For long-term support, add bbox or text-neighborhood matching once Docling exposes reliable coordinates for PPTX items.
- Empty notes still receive a low-confidence `remember/reference` classification to satisfy the "every leaf tagged" rule. If downstream consumers prefer `null` for empty notes, adjust the schema and viewer contract explicitly.
- The manifest includes real `createdAt`, `startedAt`, and `completedAt` timestamps, so byte-for-byte manifest idempotence needs a timestamp-normalized diff. `documentId` and `extractionId` are stable across reruns.
