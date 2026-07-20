#!/usr/bin/env python3
"""Scored schematic-extraction eval over a labeled corpus.

Runs the REAL extraction pipeline on each corpus drawing ``--runs`` times,
scores every run against its golden label (:mod:`schematic_eval`), and reports
per-drawing precision plus run-to-run variance — the number that tells you
whether the vision nondeterminism is within tolerance or a liability.

Usage (inside the docling-serve container, model creds in env):

    python tests/schematic_eval/run_eval.py --runs 3
    python tests/schematic_eval/run_eval.py --graphs-dir /tmp/graphs   # score
                                          # pre-extracted graphs, no model cost

Exit code is non-zero when any drawing's mean overall falls below its label's
``minOverall`` — so CI can gate on it.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

CORPUS = Path(__file__).parent / "corpus"


def _load_labels() -> dict[str, dict]:
    labels: dict[str, dict] = {}
    for path in sorted(CORPUS.glob("*.json")):
        labels[path.stem] = json.loads(path.read_text())
    return labels


def _extract_graph(pdf: Path, out_dir: Path) -> dict:
    from docling_serve.schematic.extract import extract_schematic

    result = extract_schematic(pdf, out_dir, profile="schematic", tenant_id="eval")
    return result["graph"] or {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument(
        "--pdf-dir",
        default="/tmp/reupload",
        help="Directory holding each label's sourcePdf.",
    )
    parser.add_argument(
        "--graphs-dir",
        default=None,
        help="Score pre-extracted <stem>.json graphs instead of extracting.",
    )
    parser.add_argument("--out", default=None, help="Write full JSON report here.")
    args = parser.parse_args()

    from docling_serve.schematic.schematic_eval import aggregate_runs, score_graph

    labels = _load_labels()
    if not labels:
        print("no corpus labels found", file=sys.stderr)
        return 2

    report: dict[str, dict] = {}
    ok = True
    for stem, label in labels.items():
        scores = []
        if args.graphs_dir:
            graph = json.loads((Path(args.graphs_dir) / f"{stem}.json").read_text())
            scores.append(score_graph(graph, label))
        else:
            pdf = Path(args.pdf_dir) / str(label["sourcePdf"])
            if not pdf.is_file():
                print(f"SKIP {stem}: {pdf} not found", file=sys.stderr)
                continue
            for _ in range(args.runs):
                with tempfile.TemporaryDirectory() as work:
                    graph = _extract_graph(pdf, Path(work))
                scores.append(score_graph(graph, label))
        agg = aggregate_runs(scores)
        report[stem] = {"aggregate": agg, "runs": [s.as_dict() for s in scores]}
        min_overall = label.get("minOverall", 0.7)
        drawing_ok = agg.get("overallMean", 0) >= min_overall and not agg.get(
            "anyPhantom"
        )
        ok = ok and drawing_ok
        flag = "PASS" if drawing_ok else "FAIL"
        print(
            f"[{flag}] {stem}: overall mean {agg.get('overallMean')} "
            f"(min {agg.get('overallMin')}, max {agg.get('overallMax')}, "
            f"spread {agg.get('overallSpread')}), component "
            f"{agg.get('componentScoreMean')}, passRate {agg.get('passRate')}, "
            f"phantom={agg.get('anyPhantom')}"
        )
        for s in scores:
            if s.phantomBreaches or s.missingNets or s.familyDiffs:
                print(
                    f"    - phantom={s.phantomBreaches} missingNets={s.missingNets} "
                    f"familyDiffs={s.familyDiffs}"
                )
    if args.out:
        Path(args.out).write_text(json.dumps(report, indent=2))
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
