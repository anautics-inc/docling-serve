# Experiment 5: Placeholder & Master Typography Inheritance

**Date:** 2026-05-20
**Parent:** `experiment4.md` and `experiment4-findings.md` in this folder.
**Status:** scaffolded; agent runs, verifies, writes findings.

---

## Why

Experiment4 measured how much typography is actually stated on slide-local runs. The answer, for two real fixtures:

- `TCTO_Slides_2026`: **7.06%** of runs carry an explicit font size.
- `ESCAPE Centralization`: **21.23%**.

The other 79–93% inherit size, color, and font from the **layout placeholder → master placeholder → master text styles** — a chain experiment4 deliberately did not walk. `themed_colors_resolved` was `0` for the whole corpus for the same reason: nothing slide-local references `<a:schemeClr>`; the theme reference lives in the master placeholder's run properties, one inheritance level deeper.

The data is all present in the PPTX. Experiment5 walks the rest of the chain.

## Scope — one concern

Resolve the full PowerPoint text-style inheritance chain so that `font.size`, `font.family`, `font.weight`, `font.italic`, `font.underline`, and `color` are populated for the large majority of runs — not just the slide-local minority.

**Out of scope** (unchanged from experiment4): group recursion, table-cell extraction, master-level decorative shapes, animation. Those remain experiment6+.

## The inheritance chain

For any run, PowerPoint resolves each text property by checking these sources in order, most specific first, taking the first non-null value:

| # | Source | Element |
| --- | --- | --- |
| 1 | Run | `<a:r>/<a:rPr>` |
| 2 | Paragraph default | `<a:p>/<a:pPr>/<a:defRPr>` (+ `<a:endParaRPr>`) |
| 3 | Shape list style | `<p:txBody>/<a:lstStyle>/<a:lvlNpPr>/<a:defRPr>` |
| 4 | Layout placeholder | layout shape's `<p:txBody>/<a:lstStyle>/<a:lvlNpPr>/<a:defRPr>` |
| 5 | Master placeholder | master shape's `<p:txBody>/<a:lstStyle>/<a:lvlNpPr>/<a:defRPr>` |
| 6 | Master text style | master `<p:txStyles>/<p:titleStyle\|bodyStyle\|otherStyle>/<a:lvlNpPr>/<a:defRPr>` |
| 7 | Presentation default | `presentation.xml` `<p:defaultTextStyle>/<a:lvlNpPr>/<a:defRPr>` |

Experiment4 walked 1–3. Experiment5 adds 4–7.

`N` is the paragraph's indent level (`<a:pPr lvl="N">`, 0-indexed; default 0). It selects `<a:lvl{N+1}pPr>` from each list-style / text-style source.

### Placeholder matching (steps 4–5)

A slide shape declares its placeholder via `<p:nvSpPr>/<p:nvPr>/<p:ph type="..." idx="..."/>`.

- `type` defaults to `"body"` when absent.
- `idx` is optional; it disambiguates multiple placeholders of the same type.

**Slide shape → layout placeholder:**
1. Resolve the slide's layout (slide rels → `/slideLayout`).
2. In the layout's `spTree`, find the placeholder shape:
   - If both shapes have `idx`, match on `idx`.
   - Otherwise match on `type`.
   - Treat `title` and `ctrTitle` as equivalent for matching purposes.

**Layout placeholder → master placeholder:**
1. Resolve the layout's master (layout rels → `/slideMaster`).
2. Match the layout placeholder's `<p:ph type=...>` to a master `spTree` placeholder by `type`. Masters carry one placeholder per type.

### Master text style selection (step 6)

The master's `<p:txStyles>` has three style trees. The placeholder `type` picks which one:

| Placeholder type | Master text style |
| --- | --- |
| `title`, `ctrTitle` | `<p:titleStyle>` |
| `body`, `subTitle`, `obj`, `tx` | `<p:bodyStyle>` |
| anything else, or no placeholder | `<p:otherStyle>` |

### Theme font references (`+mj-lt` / `+mn-lt`)

A `<a:latin typeface="+mj-lt"/>` is not a literal font name — it is a theme reference:

- `+mj-lt` → `theme.fontScheme.majorFont.latin`
- `+mn-lt` → `theme.fontScheme.minorFont.latin`
- `+mj-ea` / `+mn-ea` / `+mj-cs` / `+mn-cs` → the `ea` / `cs` variants

Resolve these whenever a `typeface` value starts with `+`. This is how master placeholders point at the theme without hard-coding "Calibri."

## Architecture

Two new modules + one rewrite. Everything else copies forward from experiment4 unchanged.

- `text_styles.py` — NEW. Parses a slide master's `<p:txStyles>` (title/body/other) and `presentation.xml`'s `<p:defaultTextStyle>` into level-indexed `defRPr` property maps. Cached per-master since masters are shared across many slides.
- `placeholder_resolver.py` — NEW. Given a slide part + slide rels, builds a `StyleContext` for each placeholder type: the ordered list of `<a:lstStyle>` / text-style sources (levels 3–7) that a run on that shape should inherit from. Resolving the layout and master parts is done once per slide and the master parse is memoized.
- `typography.py` — REWRITTEN. `parse_paragraphs` now takes a `StyleContext` and, for every run, walks the chain to fill any property the run itself left null. Each run gains an optional `resolvedFrom` map recording which chain level supplied each value.

`ooxml.parse_pptx` builds the per-slide `StyleContext` and threads it to `parse_paragraphs`. `manifest_builder`, `theme.py`, `background.py`, `bloom_classifier.py`, `semantics.py` are unchanged.

## Data shape additions

### `run.resolvedFrom` (new, optional)

```jsonc
{
  "text": "TIME COMPLIANCE TECHNICAL ORDER",
  "font": {
    "family": "Arial",
    "size": 44.0,
    "weight": "bold",
    "italic": false,
    "underline": "none"
  },
  "color": "#1F4E79",
  "resolvedFrom": {
    "family": "masterTextStyle",
    "size": "masterTextStyle",
    "weight": "layoutPlaceholder",
    "italic": "default",
    "underline": "default",
    "color": "masterTextStyle"
  }
}
```

`resolvedFrom` values are one of:
`run | paragraph | shapeListStyle | layoutPlaceholder | masterPlaceholder | masterTextStyle | presentationDefault | themeFont | default | unresolved`.

- `default` means the property is a documented PowerPoint default (e.g. `italic=false`, `underline="none"`) that no level explicitly set.
- `unresolved` means the chain produced nothing — the renderer falls back to its own default. After experiment5 this should be rare; track its frequency in findings.

`resolvedFrom` is the auditable signal. It lets the findings report exactly how much each inheritance level contributed and makes a regression in the resolver visible.

### `manifest.theme` — unchanged

The theme block from experiment4 stays as-is; experiment5 only *consumes* it (for `+mj-lt` resolution).

## PRs

### PR1 — master & presentation text styles

- New `deep_document/text_styles.py`:
  - `parse_master_text_styles(zf, master_part) -> {"title": {...}, "body": {...}, "other": {...}}` where each value is `{level_index: defrpr_props}`.
  - `parse_presentation_default_style(zf) -> {level_index: defrpr_props}`.
  - `defrpr_props` is the same dict shape experiment4's `_font_from_rpr` produces, plus `color`.
- Tests: `test_text_styles.py` — parse a real master, assert `titleStyle` level-0 has a size.

### PR2 — placeholder resolver

- New `deep_document/placeholder_resolver.py`:
  - `StyleContext` dataclass: holds the ordered `defRPr` sources for one shape (levels 3–7) plus the theme.
  - `build_slide_style_contexts(zf, slide_part, slide_rels, theme) -> dict[placeholder_key, StyleContext]` — resolves layout + master once, memoizes the master parse.
  - Placeholder matching per the algorithm above. Unmatched placeholders fall back to the master text style for their type; a shape with no placeholder uses `otherStyle`.
- Tests: `test_placeholder_resolver.py` — a known fixture: a title shape resolves a non-empty `StyleContext` whose master-text-style source is `titleStyle`.

### PR3 — typography resolution through the chain

- Rewrite `typography.parse_paragraphs(tx_body, theme, style_context)`:
  - For each run, start from run `<a:rPr>`, then fill nulls from paragraph defRPr, then walk the `StyleContext` levels in order.
  - Resolve `+mj-lt` / `+mn-lt` font references against `theme.fontScheme`.
  - Emit `resolvedFrom` per run.
  - Apply documented defaults last (`italic=false`, `underline="none"`, `weight="normal"`) tagged `resolvedFrom="default"`.
- `ooxml.parse_pptx` builds contexts via `build_slide_style_contexts` and passes the right context per shape (keyed on placeholder type/idx; `otherStyle` context for non-placeholder shapes).
- Schema: add optional `resolvedFrom` object to the run schema.
- Tests: `test_typography_inheritance.py` — a run with an empty `<a:rPr>` inherits size from a synthetic master text style; `resolvedFrom.size == "masterTextStyle"`.

## Definition of Done

- All 7 fixtures run to `status=complete`.
- `runs_with_size` ≥ **90%** on every fixture, including TCTO and ESCAPE. (If a fixture sits below 90%, the findings must explain which inheritance level is still missing — e.g. a deck that references a second master.)
- `runs_with_color` ≥ **90%** on every fixture.
- `themed_colors_resolved` > 0 on at least one fixture — proof the `<a:schemeClr>` references in master placeholders now resolve through the theme.
- `unresolved` resolvedFrom values are < 5% of runs corpus-wide.
- New tests pass: `test_text_styles.py`, `test_placeholder_resolver.py`, `test_typography_inheritance.py`.
- All experiment4 regressions copied forward still pass.
- `experiment5-findings.md` reports a before/after table:
  `fixture | runs_with_size_v4 | runs_with_size_v5 | runs_with_color_v5 | dominant_resolution_level`.

## Sanity checks

```bash
# 1. Size coverage per fixture
uv run python -c "
import json, pathlib
for m in sorted(pathlib.Path('tests/experiment5/out').rglob('manifest.json')):
    doc = json.loads(m.read_text())
    runs = with_size = 0
    for u in doc['units']:
        for b in u['blocks']:
            for p in b.get('paragraphs', []) or []:
                for r in p.get('runs', []):
                    runs += 1
                    if (r.get('font') or {}).get('size') is not None:
                        with_size += 1
    pct = 100 * with_size / runs if runs else 0
    print(f'{doc[\"source\"][\"originalFileName\"][:40]:42s} {with_size:5d}/{runs:5d}  {pct:5.1f}%')
"

# 2. resolvedFrom distribution
uv run python -c "
import json, pathlib
from collections import Counter
c = Counter()
for m in pathlib.Path('tests/experiment5/out').rglob('manifest.json'):
    doc = json.loads(m.read_text())
    for u in doc['units']:
        for b in u['blocks']:
            for p in b.get('paragraphs', []) or []:
                for r in p.get('runs', []):
                    rf = r.get('resolvedFrom') or {}
                    c[rf.get('size', 'missing')] += 1
print('size resolved from:', dict(c.most_common()))
"
```

## File map

```
tests/experiment5/
├── README.md
├── .gitignore
├── run_experiment.py                         # copy from experiment4; OUT_ROOT redirected
├── deep_document/
│   ├── __init__.py
│   ├── bloom_classifier.py                    # copy unchanged
│   ├── theme.py                               # copy unchanged
│   ├── background.py                          # copy unchanged
│   ├── semantics.py                           # copy unchanged
│   ├── text_styles.py                         # NEW
│   ├── placeholder_resolver.py                # NEW
│   ├── typography.py                          # REWRITTEN — chain resolution
│   ├── ooxml.py                               # builds + threads StyleContext
│   ├── manifest_builder.py                    # copy unchanged (theme already threaded)
│   └── schema.py                              # run schema gains optional resolvedFrom
├── schema/
│   └── deep-document-manifest.schema.json     # regenerated
└── tests/
    ├── __init__.py
    ├── conftest.py
    ├── test_text_styles.py                    # NEW
    ├── test_placeholder_resolver.py           # NEW
    ├── test_typography_inheritance.py         # NEW
    └── (experiment4 regression tests copied forward)
```
