"""Check whether two runs of the same config produced the same outputs.

    python -m evals.determinism baseline-extractive baseline-extractive-rerun

Reports three levels, because "not bit-identical" and "behaves differently" are different things:

  1. exact      every field equal except wall-clock latency
  2. tolerant   equal after rounding floats to 1e-9 (absorbs float summation-order noise)
  3. behaviour  same retrieved chunk ids (set and order), cited chunks, refusal flag and answer

Exit code 0 only if the runs are equal at level 2 (the level the metrics depend on).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from evals.run import RUNS_DIR

VOLATILE = {"latency_ms"}
FLOAT_DIGITS = 9


def _canon(obj: Any, digits: int | None) -> Any:
    if isinstance(obj, float):
        return obj if digits is None else round(obj, digits)
    if isinstance(obj, dict):
        return {k: _canon(v, digits) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_canon(v, digits) for v in obj]
    return obj


def _strip(rec: dict[str, Any], digits: int | None) -> dict[str, Any]:
    out = dict(rec)
    if out.get("result"):
        out["result"] = {k: v for k, v in out["result"].items() if k not in VOLATILE}
    return _canon(out, digits)


def _behaviour(rec: dict[str, Any]) -> Any:
    r = rec["result"]
    if r is None:
        return rec["error"]
    return (
        [x["chunk_id"] for x in r["retrieved"]],
        r["cited_chunk_ids"],
        r["refused"],
        r["answer"],
    )


def diff_runs(
    a: list[dict[str, Any]],
    b: list[dict[str, Any]],
    *,
    digits: int | None = FLOAT_DIGITS,
) -> list[str]:
    """Scenario ids that differ. `digits=None` compares exactly; default tolerates float noise."""
    if [r["scenario_id"] for r in a] != [r["scenario_id"] for r in b]:
        return ["scenario id lists differ"]
    return [
        ra["scenario_id"]
        for ra, rb in zip(a, b, strict=True)
        if _strip(ra, digits) != _strip(rb, digits)
    ]


def behaviour_diff(a: list[dict[str, Any]], b: list[dict[str, Any]]) -> list[str]:
    return [
        ra["scenario_id"] for ra, rb in zip(a, b, strict=True) if _behaviour(ra) != _behaviour(rb)
    ]


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
    same_inputs = (
        ma["corpus"]["sha256"] == mb["corpus"]["sha256"]
        and ma["golden_sha256"] == mb["golden_sha256"]
        and ma["config"] == mb["config"]
    )
    exact = diff_runs(ra, rb, digits=None)
    tolerant = diff_runs(ra, rb)
    behaviour = behaviour_diff(ra, rb)
    n = len(ra)
    print(f"same corpus/golden/config: {same_inputs}")
    print(f"1. exact      : {n - len(exact)}/{n} scenarios identical")
    print(
        f"2. tolerant   : {n - len(tolerant)}/{n} identical (floats rounded to 1e-{FLOAT_DIGITS})"
    )
    print(f"3. behaviour  : {n - len(behaviour)}/{n} same ids/order, citations, refusal, answer")
    if tolerant:
        print(f"   differ at level 2: {tolerant}")
    if behaviour:
        print(f"   differ at level 3: {behaviour}")
    return 0 if (same_inputs and not tolerant) else 1


if __name__ == "__main__":
    raise SystemExit(main())
