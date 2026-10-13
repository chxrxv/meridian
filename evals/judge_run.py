"""Run the LLM judge over a saved run.  GATED: do not run until the project owner confirms that
the blind human labels are in.

    python -m evals.judge_run TAG --labels-confirmed

Refuses to start unless (1) --labels-confirmed is passed AND (2) evals/judge_labels.jsonl has every
human label filled in. Writes evals/runs/TAG/judge.jsonl incrementally (resumable) and
judge_meta.json. Judge numbers remain UNVALIDATED until `evals.labels agreement` is computed.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

from evals import judge as judge_mod
from evals.golden import GOLDEN_PATH, load_golden
from evals.metrics import build_context
from evals.provenance import ROOT, golden_sha256
from evals.run import RUNS_DIR

LABELS = Path(__file__).resolve().parent / "judge_labels.jsonl"


def labels_complete(path: Path = LABELS) -> tuple[bool, str]:
    if not path.exists():
        return False, "evals/judge_labels.jsonl does not exist"
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    if len(rows) < 10:
        return False, f"only {len(rows)} labelled scenarios (need >= 10)"
    missing = [
        r["scenario_id"]
        for r in rows
        if r.get("human_faithful") is None or r.get("human_correct") is None
    ]
    if missing:
        return False, f"{len(missing)} scenarios still unlabelled: {missing}"
    return True, "ok"


def main(argv: list[str] | None = None, *, judge: judge_mod.Judge | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("tag")
    ap.add_argument("--labels-confirmed", action="store_true")
    ap.add_argument("--runs-dir", default=str(RUNS_DIR))
    ap.add_argument("--labels", default=str(LABELS))
    ap.add_argument("--golden", default=str(GOLDEN_PATH))
    args = ap.parse_args(argv)

    if not args.labels_confirmed:
        print(
            "REFUSING: pass --labels-confirmed only after the owner says the human labels are in.",
            file=sys.stderr,
        )
        return 3
    ok, why = labels_complete(Path(args.labels))
    if not ok:
        print(f"REFUSING: human labels incomplete: {why}", file=sys.stderr)
        return 3

    run_dir = Path(args.runs_dir) / args.tag
    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    records = [
        json.loads(x)
        for x in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    scenarios = {s.id: s for s in load_golden(Path(args.golden))}
    if meta["golden_sha256"] != golden_sha256(Path(args.golden)):
        print("REFUSING: run was produced against a different golden-set version", file=sys.stderr)
        return 3

    j = judge or judge_mod.make_judge()
    judge_mod.check_independent(j.cfg.model, meta["models"]["answerer"])
    ctx = build_context(ROOT / "data", meta["config"]["chunking"])

    out = run_dir / "judge.jsonl"
    done = {
        json.loads(x)["scenario_id"]
        for x in (out.read_text(encoding="utf-8").splitlines() if out.exists() else [])
        if x.strip()
    }
    status, n_new = "complete", 0
    with out.open("a", encoding="utf-8") as fh:
        for rec in records:
            if rec["result"] is None or rec["scenario_id"] in done:
                continue
            s, r = scenarios[rec["scenario_id"]], rec["result"]
            chunks = [ctx.chunk_text.get(x["chunk_id"], "") for x in r["retrieved"]]
            try:
                v = j.judge_one(s, r["answer"], chunks)
            except judge_mod.QuotaExhausted as e:
                status = f"incomplete: {e}"
                break
            fh.write(json.dumps(dataclasses.asdict(v)) + "\n")
            fh.flush()
            n_new += 1
    total = sum(1 for x in out.read_text(encoding="utf-8").splitlines() if x.strip())
    (run_dir / "judge_meta.json").write_text(
        json.dumps(
            {
                "status": status,
                "backend": j.cfg.backend,
                "model": j.cfg.model,
                "ollama_fallback_model": j.cfg.ollama_model,
                "rpm": j.cfg.rpm,
                "judged": total,
                "of": sum(1 for r in records if r["result"] is not None),
                "this_invocation": {"judged": n_new, **j.stats},
                "billing": "free tier (API key only)",
                "validated": False,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"judge {status}: {total} verdicts in {out}; stats {j.stats}")
    return 0 if status == "complete" else 4


if __name__ == "__main__":
    raise SystemExit(main())
