"""Human labels for validating the LLM judge.

    python -m evals.labels sample --tag baseline-extractive     # writes the sheet + label template
    python -m evals.labels agreement --tag <tag-with-judge-run> # after labels are filled in

`sample` picks 10 scenarios deterministically across categories and writes:
  evals/judge_label_sheet.md   what to read (question, reference, answer, retrieved context)
  evals/judge_labels.jsonl     what to fill in: human_faithful / human_correct (true/false)
Agreement is reported only over scenarios that have both a human label and a judge verdict.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from evals.golden import GOLDEN_PATH, load_golden
from evals.metrics import build_context
from evals.provenance import ROOT
from evals.report import load_run
from evals.run import RUNS_DIR

LABELS = Path(__file__).resolve().parent / "judge_labels.jsonl"
SHEET = Path(__file__).resolve().parent / "judge_label_sheet.md"
QUOTA = {
    "single_doc": 2,
    "multi_hop": 1,
    "acl_boundary": 1,
    "unanswerable": 2,
    "ambiguous": 1,
    "prompt_injection": 1,
    "pii": 1,
    "write_action": 1,
}


def sample(tag: str, runs_dir: Path) -> None:
    run = load_run(tag, runs_dir)
    scen = {s.id: s for s in load_golden(GOLDEN_PATH)}
    ctx = build_context(ROOT / "data", run.meta["config"]["chunking"])
    rng = random.Random(7)
    by_cat: dict[str, list[dict]] = {}
    for rec in run.records:
        if rec["result"] is not None:
            by_cat.setdefault(rec["category"], []).append(rec)
    picked = []
    for cat, n in QUOTA.items():
        picked += rng.sample(by_cat.get(cat, []), min(n, len(by_cat.get(cat, []))))

    sheet = [
        f"# Judge label sheet (run `{tag}`)\n",
        "For each scenario fill `human_faithful` (is every claim in the answer supported by the "
        "retrieved context? a refusal is faithful) and `human_correct` (does the answer match the "
        "reference / required behaviour?) in `evals/judge_labels.jsonl`.\n",
    ]
    labels = []
    for i, rec in enumerate(picked, 1):
        s, r = scen[rec["scenario_id"]], rec["result"]
        sheet.append(f"## {i}. `{s.id}` ({s.category}, user `{s.user_id}`)\n")
        sheet.append(
            f"**Question:** {s.message}\n\n**Reference / required behaviour:** {s.expected}\n"
        )
        sheet.append(
            f"**Answer** (refused={r['refused']}, confidence={r['confidence']:.2f}):\n"
            f"\n> {r['answer']}\n"
        )
        sheet.append("**Retrieved context:**\n")
        for x in r["retrieved"]:
            text = " ".join(ctx.chunk_text.get(x["chunk_id"], "").split())
            sheet.append(f"- `{x['chunk_id']}`: {text}")
        sheet.append("")
        labels.append(
            {"scenario_id": s.id, "human_faithful": None, "human_correct": None, "notes": ""}
        )
    SHEET.write_text("\n".join(sheet), encoding="utf-8")
    LABELS.write_text("\n".join(json.dumps(x) for x in labels) + "\n", encoding="utf-8")
    print(f"wrote {SHEET} and {LABELS} ({len(labels)} scenarios)")
    print("\n".join(sheet))


def agreement(tag: str, runs_dir: Path) -> dict:
    labels = {
        j["scenario_id"]: j
        for j in map(json.loads, LABELS.read_text(encoding="utf-8").splitlines())
        if j
    }
    jpath = runs_dir / tag / "judge.jsonl"
    if not jpath.exists():
        raise SystemExit(
            f"no judge verdicts in {jpath}: run `evals.run --judge` with credentials first"
        )
    judged = {
        j["scenario_id"]: j
        for j in map(json.loads, jpath.read_text(encoding="utf-8").splitlines())
        if j
    }
    out = {}
    for human_key, judge_key in (("human_faithful", "faithful"), ("human_correct", "correct")):
        pairs = [
            (labels[i][human_key], judged[i][judge_key])
            for i in labels
            if i in judged and labels[i][human_key] is not None and judged[i][judge_key] is not None
        ]
        out[judge_key] = {
            "n": len(pairs),
            "agreement": (sum(a == b for a, b in pairs) / len(pairs)) if pairs else None,
        }
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["sample", "agreement"])
    ap.add_argument("--tag", required=True)
    ap.add_argument("--runs-dir", default=str(RUNS_DIR))
    args = ap.parse_args(argv)
    if args.cmd == "sample":
        sample(args.tag, Path(args.runs_dir))
    else:
        print(json.dumps(agreement(args.tag, Path(args.runs_dir)), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
