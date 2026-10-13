"""Check that two runs of the same config produced identical outputs.

    python -m evals.determinism baseline-extractive baseline-extractive-rerun

Compares every field of every result except wall-clock timings. Exit code 0 = identical.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evals.run import RUNS_DIR

VOLATILE = {"latency_ms"}


def _strip(rec: dict[str, Any]) -> dict[str, Any]:
    out = dict(rec)
    if out.get("result"):
        out["result"] = {k: v for k, v in out["result"].items() if k not in VOLATILE}
    return out


def diff_runs(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[str]:
    problems = []
    if [r["scenario_id"] for r in a] != [r["scenario_id"] for r in b]:
        problems.append("scenario id lists differ")
        return problems
    for ra, rb in zip(a, b, strict=True):
        if _strip(ra) != _strip(rb):
            problems.append(ra["scenario_id"])
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag_a")
    ap.add_argument("tag_b")
    ap.add_argument("--runs-dir", default=str(RUNS_DIR))
    args = ap.parse_args(argv)

    def load(tag: str):
        d = Path(args.runs_dir) / tag
        recs = [
            json.loads(x)
            for x in (d / "results.jsonl").read_text(encoding="utf-8").splitlines()
            if x.strip()
        ]
        return recs, json.loads((d / "meta.json").read_text(encoding="utf-8"))

    (ra, ma), (rb, mb) = load(args.tag_a), load(args.tag_b)
    problems = diff_runs(ra, rb)
    same_inputs = (
        ma["corpus"]["sha256"] == mb["corpus"]["sha256"]
        and ma["golden_sha256"] == mb["golden_sha256"]
        and ma["config"] == mb["config"]
    )
    print(f"same corpus/golden/config: {same_inputs}")
    if problems:
        print(f"NOT deterministic: {len(problems)} differing scenarios: {problems}")
        return 1
    print(f"deterministic: {len(ra)} scenarios identical (excluding latency_ms)")
    return 0 if same_inputs else 1


if __name__ == "__main__":
    raise SystemExit(main())
