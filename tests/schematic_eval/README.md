# Schematic extraction eval

A scored, corpus-driven regression gate for the schematic pipeline. Answers
the question unit tests cannot: *did we read this drawing correctly, and how
much does the answer wobble run to run?*

## Layout

- `corpus/*.json` — one golden label per drawing (ground truth). Tolerant
  about counts the drawing renders ambiguously (ground-glyph count), strict
  about failure modes that poison downstream artifacts (`forbiddenFamilies`,
  polarity-bearing `namedNets`).
- `run_eval.py` — extracts each `sourcePdf` `--runs` times, scores every run
  (`docling_serve.schematic.schematic_eval.score_graph`), reports per-drawing
  mean/min/max/spread and pass-rate. Non-zero exit when any drawing's mean
  `overall` falls below its label's `minOverall` — CI-gateable.

## Running (inside the docling-serve container, model creds in env)

    python tests/schematic_eval/run_eval.py --runs 3 --pdf-dir /path/to/pdfs

Or score pre-extracted graphs with no model cost:

    python tests/schematic_eval/run_eval.py --graphs-dir /tmp/graphs

The pure scorer is unit-tested model-free in `tests/test_schematic_eval.py`.

## Baseline (2 runs each, hardened pipeline, 2026-07-08)

| drawing        | kind          | overall mean | spread | component | phantom |
|----------------|---------------|--------------|--------|-----------|---------|
| figure32       | analog-power  | 0.925        | 0.075  | 1.00      | none    |
| main_schematic | digital-logic | 0.711        | 0.050  | 0.84      | none    |

`figure32` is phantom-free (the reviewed ground-vs-capacitor failure is gone);
`main_schematic` proves the opposite circuit type extracts without inflation.
Spread quantifies the vision nondeterminism — track it as the corpus grows.

## Extending

Drop a `<name>.pdf` in the pdf dir and author `corpus/<name>.json`. Cover the
domains you deploy against (wiring/harness, P&ID, ladder) — the thresholds in
`schematic_tuning.SchematicTuning` are env-overridable so a sweep can tune
them against this corpus instead of by hand.
