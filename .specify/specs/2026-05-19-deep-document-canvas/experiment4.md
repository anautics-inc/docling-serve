# Experiment 4: Typography, Theme, and Slide Background

**Date:** 2026-05-20
**Parent:** `experiment3.md` and `experiment3-findings.md` in this folder.
**Status:** scaffolded; agent runs, verifies, writes findings.

---

## Goal

Make the deep-document manifest carry enough presentation data that a downstream renderer (tldraw, custom React, SVG server, print) can reconstruct the slide so it **looks like the source PowerPoint**, not a wireframe.

Experiment3 nailed structure (where things are, what they contain, how they're tagged). Experiment4 adds presentation (what they look like): theme colors, run-level typography, paragraph styling, and slide backgrounds. No external renderer. No new dependencies. All data is parsed from OOXML that we're already walking.

## Non-goals (explicit)

- Group recursion (`<p:grpSp>` children) — deferred to experiment5.
- Table cell extraction (per-cell text/style) — deferred to experiment5.
- Master-level shapes (corporate logos placed on master, not slides) — deferred to experiment5.
- Animation, transition, slide notes formatting — never. Out of scope per PRD §181.
- Visual rendering itself. We extract; consumers render.

## Architecture

Three new modules in `deep_document/`:

- `theme.py` — parses `ppt/theme/themeN.xml`. Emits a single `manifest.theme` block with `colorScheme` + `fontScheme`. Called once per deck.
- `typography.py` — parses one `<p:txBody>` into `paragraphs: [{ alignment, indentLevel, bullet, runs: [{text, font, color, weight, ...}] }]`. Resolves theme references using the parsed theme.
- `background.py` — walks slide → layout → master to find the slide's effective background (solid, gradient, image, or none).

Existing modules update:

- `ooxml.py` — for every `<p:sp>` text block, attach `paragraphs` produced by `typography.py`. For every unit, attach `background`. Group/table extraction unchanged (deferred).
- `manifest_builder.py` — call `theme.parse_theme` once per deck, pass to typography parser, emit `manifest.theme` at top level.
- `schema.py` — add optional `paragraphs`, `unit.background`, `manifest.theme`. All optional so experiment3 manifests stay compatible.

The manifest schemaVersion stays at **2.0**. New fields are additive — `additionalProperties` on the relevant sub-schemas already permits them; the schema just gains explicit definitions so consumers can rely on shape.

## Data shapes added

### `manifest.theme` (new, top-level)

```jsonc
{
  "themeFile": "ppt/theme/theme1.xml",
  "themeName": "Office",
  "colorScheme": {
    "tx1": "#000000",         // text 1 (resolved from <a:dk1>)
    "bg1": "#FFFFFF",         // background 1 (resolved from <a:lt1>)
    "tx2": "#44546A",
    "bg2": "#E7E6E6",
    "accent1": "#5B9BD5",
    "accent2": "#ED7D31",
    "accent3": "#A5A5A5",
    "accent4": "#FFC000",
    "accent5": "#4472C4",
    "accent6": "#70AD47",
    "hlink": "#0563C1",
    "folHlink": "#954F72"
  },
  "fontScheme": {
    "name": "Office",
    "majorFont": { "latin": "Calibri Light", "ea": "", "cs": "" },
    "minorFont": { "latin": "Calibri",       "ea": "", "cs": "" }
  }
}
```

### `units[].background` (new)

```jsonc
{
  "kind": "solid" | "gradient" | "image" | "inherited" | "none",
  "color": "#FFFFFF" | null,            // for kind=solid
  "assetId": "asset-..." | null,        // for kind=image
  "source": "slide" | "layout" | "master"
}
```

Background lookup order: slide's `<p:bg>` → layout's `<p:bg>` → master's `<p:bg>`. The first non-empty wins, and `source` records which level provided it.

### `units[].blocks[].paragraphs` (new, optional)

```jsonc
[
  {
    "alignment": "left" | "center" | "right" | "justify" | null,
    "indentLevel": 0,
    "bullet": null | {
      "kind": "char" | "number" | "none",
      "char": "•" | "-" | "*",
      "numberFormat": "arabicPeriod" | "romanLcParenBoth" | null
    },
    "runs": [
      {
        "text": "TIME COMPLIANCE TECHNICAL ORDER",
        "font": {
          "family": "Calibri",
          "size": 36.0,         // points (decoded from PPTX hundredths)
          "weight": "bold" | "normal",
          "italic": true | false,
          "underline": "single" | "double" | "none"
        },
        "color": "#1F2937"      // resolved hex; theme refs already de-referenced
      }
    ]
  }
]
```

`block.text` stays — it's the joined plain text. Consumers that don't need typography can ignore `paragraphs` entirely.

## Theme color resolution

OOXML expresses text color in three forms — all three must resolve to an RGB hex string:

| Form | Example | Resolution |
| --- | --- | --- |
| Direct RGB | `<a:srgbClr val="5B9BD5"/>` | `"#5B9BD5"` |
| System color | `<a:sysClr val="windowText" lastClr="000000"/>` | use `lastClr` |
| Theme reference | `<a:schemeClr val="tx1"/>` | lookup in `theme.colorScheme["tx1"]` |

The theme reference names in `<a:schemeClr val="...">` differ slightly from the element names in `<a:clrScheme>`:

| Scheme element | Reference name |
| --- | --- |
| `dk1` | `tx1` |
| `lt1` | `bg1` |
| `dk2` | `tx2` |
| `lt2` | `bg2` |
| `accent1`–`accent6` | same |
| `hlink`, `folHlink` | same |

Tint/shade modifiers (`<a:lumMod val="75000"/>` + `<a:lumOff val="25000"/>`) modify the resolved color. Experiment4 captures the base color only; tint/shade is a follow-up.

## Font size

PPTX stores `sz` as 100× points: `sz="2400"` means 24pt. Convert to float points on extract; the manifest stores `font.size: 24.0` for downstream simplicity.

## Inheritance — what experiment4 does and doesn't do

Real PowerPoint resolves typography through a six-level inheritance chain:

```
slide run → slide paragraph → slide list-style → layout placeholder → master placeholder → theme
```

Experiment4 only walks the **first three levels** (slide run, slide paragraph, slide list-style). If a run inherits a 36pt font from the master placeholder and doesn't restate it locally, experiment4 will emit `font.size: null` for that run.

This is intentional: full inheritance walking is another 400–600 lines of XML traversal and adds complexity that doesn't pay off until masters become a renderer concern. A downstream renderer is free to display `null` as "use renderer default" — which is what PowerPoint effectively does.

Document the gap explicitly in `experiment4-findings.md`. The cost shows up as a fraction-of-runs-with-resolved-size column in the comparison CSV.

## Slide background

The new `units[].background` field records the *first* non-empty `<p:bg>` encountered along the slide → layout → master walk. Forms:

- `<p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill>` → `{kind:"solid", color:"#FFFFFF", source:"slide"}`
- `<p:bgPr><a:blipFill><a:blip r:embed="rId1"/></a:blipFill>` → `{kind:"image", assetId:"asset-<sha8>", source:"slide"}` — the image binary is added to `manifest.assets` with `kind:"slide_background"`.
- `<p:bgRef><a:schemeClr val="bg1"/>` → `{kind:"solid", color:"<theme.bg1>", source:"slide"}` — resolved via theme.
- Nothing found anywhere → `{kind:"none", source:"master"}` (renderer defaults to white).

## PRs

### PR1 — theme parsing

- New `deep_document/theme.py` with `parse_theme(zf) -> dict`.
- Wire into `manifest_builder.build_manifest_for_fixture`: parse once, attach as `manifest.theme`, pass to typography parser.
- Schema: `manifest.theme` is required (object); `colorScheme` populates all 12 standard slots even when the deck omits some (default to scheme `name="Office"` palette).
- Tests: `test_theme_resolution.py` — known fixture, assert tx1/bg1/accent1 resolved hexes.

### PR2 — run-level typography

- New `deep_document/typography.py` with `parse_paragraphs(tx_body_elem, theme: dict) -> list[paragraph_dict]`.
- Update `ooxml.parse_pptx` to call `typography.parse_paragraphs` for every text-bearing shape.
- Add `block.paragraphs` to the schema (optional array).
- Tests: `test_typography_parses_runs.py` — assert font, size, weight, italic, color resolution per run.

### PR3 — slide background

- New `deep_document/background.py` with `slide_background(zf, slide_part, slide_rels, theme, asset_registry) -> dict`.
- Update `ooxml.parse_pptx` to attach `unit.background` per slide.
- Add `units[].background` to the schema; emit `kind:"slide_background"` assets in `manifest.assets[]` when the background is an image.
- Tests: `test_slide_background.py` — assert correct source resolution and image-asset inclusion.

## Definition of Done

- All 7 fixtures run to `status=complete`.
- `manifest.theme.colorScheme` has all 12 keys with valid `#RRGGBB` hex on every fixture.
- For at least one fixture, ≥80% of text runs have `font.size != null`. (Decks that inherit heavily from masters may sit lower — record the per-fixture coverage in findings.)
- For at least one fixture, at least one slide has `background.kind == "image"` AND the asset exists in `manifest.assets[]` with `kind == "slide_background"`.
- New tests pass: `test_theme_resolution.py`, `test_typography_parses_runs.py`, `test_slide_background.py`.
- Existing experiment3 regressions still pass (copy them forward unchanged).
- `experiment4-findings.md` reports:
  - Per-fixture coverage: `themed_colors_resolved`, `runs_with_size`, `runs_with_color`, `slides_with_explicit_background`, `slides_inheriting_background`.
  - One screenshot or HTML render of a single slide using the new data (the agent can sketch this as a small auxiliary script — not part of the contract, but proof the data is sufficient).

## Sanity checks

```bash
# 1. Theme present on every manifest
uv run python -c "
import json, pathlib
for m in pathlib.Path('tests/experiment4/out').rglob('manifest.json'):
    doc = json.loads(m.read_text())
    assert 'theme' in doc, f'no theme on {m.name}'
    cs = doc['theme']['colorScheme']
    assert set(cs) >= {'tx1','bg1','accent1','accent2','accent3','accent4','accent5','accent6','hlink','folHlink'}
    print(m.parent.parent.parent.name, '->', cs['tx1'], cs['bg1'], cs['accent1'])
"

# 2. At least one slide has an image background somewhere in the corpus
uv run python -c "
import json, pathlib
hits = []
for m in pathlib.Path('tests/experiment4/out').rglob('manifest.json'):
    doc = json.loads(m.read_text())
    for u in doc['units']:
        if u.get('background', {}).get('kind') == 'image':
            hits.append((m.name, u['unitId']))
print(f'image-background slides: {len(hits)}', hits[:5])
"

# 3. Run-level typography coverage
uv run python -c "
import json, pathlib
for m in pathlib.Path('tests/experiment4/out').rglob('manifest.json'):
    doc = json.loads(m.read_text())
    runs, with_size = 0, 0
    for u in doc['units']:
        for b in u['blocks']:
            for p in b.get('paragraphs', []) or []:
                for r in p.get('runs', []):
                    runs += 1
                    if (r.get('font') or {}).get('size') is not None:
                        with_size += 1
    print(m.parent.parent.parent.name, '->', f'{with_size}/{runs} runs with size')
"
```

## File map

```
tests/experiment4/
├── README.md
├── .gitignore                                # out/
├── run_experiment.py                         # copy from experiment3; OUT_ROOT redirected
├── deep_document/
│   ├── __init__.py
│   ├── bloom_classifier.py                   # copy unchanged
│   ├── ooxml.py                              # extended: paragraphs + background per unit
│   ├── theme.py                              # NEW
│   ├── typography.py                         # NEW
│   ├── background.py                         # NEW
│   ├── semantics.py                          # copy unchanged
│   ├── manifest_builder.py                   # wires theme in, threads it to ooxml
│   └── schema.py                             # optional fields added
├── schema/
│   └── deep-document-manifest.schema.json    # regenerated
└── tests/
    ├── __init__.py
    ├── conftest.py                           # copy from experiment3
    ├── test_theme_resolution.py              # NEW
    ├── test_typography_parses_runs.py        # NEW
    ├── test_slide_background.py              # NEW
    └── (experiment3 regression tests copied forward)
```
