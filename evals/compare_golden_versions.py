"""Score the SAME saved outputs under golden v1 (+ legacy write rule) and golden v2, side by side.

    python -m evals.compare_golden_versions baseline-extractive-golden-v1

Because the outputs are identical, every difference is caused by the golden edits / the write rule,
not by the pipeline. Each changed scenario is attributed to its documented cause (CHANGELOG.md);
anything else is flagged UNEXPECTED.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from evals import metrics as M
from evals.golden import GOLDEN_PATH, load_golden
from evals.provenance import ROOT
from evals.report import CATEGORIES, load_run
from evals.run import RUNS_DIR

V1_PATH = Path(__file__).resolve().parent / "archive" / "golden.v1.jsonl"
CAUSE = {"acl-01b": "golden edit: expectation (Rule 1)"}


def score_all(tag: str, golden: Path, write_rule: str, runs_dir: Path = RUNS_DIR):
    run = load_run(tag, runs_dir)
    scen = {s.id: s for s in load_golden(golden)}
    ctx = M.build_context(ROOT / "data", run.meta["config"]["chunking"])
    rows = [
        M.score_scenario(scen[r["scenario_id"]], r["result"], ctx, write_rule=write_rule)
        for r in run.records
    ]
    return rows, scen


def compare(tag: str, runs_dir: Path = RUNS_DIR) -> dict:
    v1_rows, v1 = score_all(tag, V1_PATH, "v1", runs_dir)
    v2_rows, v2 = score_all(tag, GOLDEN_PATH, "v2", runs_dir)
    by_cat = {}
    for cat in [None, *CATEGORIES]:
        a = [r for r in v1_rows if cat in (None, r["category"])]
        b = [r for r in v2_rows if cat in (None, r["category"])]
        by_cat[cat or "overall"] = (
            sum(r["task_success"] for r in a),
            sum(r["task_success"] for r in b),
            len(a),
        )
    changed = []
    for a, b in zip(v1_rows, v2_rows, strict=True):
        extra = {}
        for k in ("refusal_correct", "task_success"):
            if a[k] != b[k]:
                extra[k] = (a[k], b[k])
        # refusal_correct changes only if should_refuse changed (acl-01b)
        if extra:
            sid = a["id"]
            cause = CAUSE.get(sid) or (
                "write rule v2 (golden v2 Rule 2)"
                if a["category"] == "write_action"
                else "UNEXPECTED"
            )
            changed.append({"id": sid, "cause": cause, **extra})
    return {"by_cat": by_cat, "changed": changed, "v1": v1_rows, "v2": v2_rows}


def _fmt(t: tuple[bool, bool] | None) -> str:
    if t is None:
        return "unchanged"
    return f"{'pass' if t[0] else 'fail'} -> {'pass' if t[1] else 'fail'}"


def to_markdown(res: dict) -> str:
    L = [
        "| category | n | task success, golden v1 (original) | task success, golden v2 (revised) |",
        "|---|---|---|---|",
    ]
    for cat, (a, b, n) in res["by_cat"].items():
        name = f"**{cat}**" if cat == "overall" else cat
        L.append(f"| {name} | {n} | {a}/{n} ({a / n * 100:.1f}%) | {b}/{n} ({b / n * 100:.1f}%) |")
    L.append("")
    L.append("Scenarios whose result changed (identical raw outputs; only the scoring differs):\n")
    L.append("| scenario | cause | task_success v1 -> v2 | refusal_correct v1 -> v2 |")
    L.append("|---|---|---|---|")
    for c in res["changed"]:
        ts = c.get("task_success")
        rc = c.get("refusal_correct")
        L.append(f"| `{c['id']}` | {c['cause']} | {_fmt(ts)} | {_fmt(rc)} |")
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--runs-dir", default=str(RUNS_DIR))
    a = ap.parse_args(argv)
    print(to_markdown(compare(a.tag, Path(a.runs_dir))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
