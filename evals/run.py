"""Run the golden set through `rag.entry.answer()` and save raw outputs.

    python -m evals.run --tag baseline-extractive
    python -m evals.run --subset 6 --tag smoke --generator mock
    python -m evals.run --tag t --corpus no-fixtures --exclude-categories prompt_injection

Writes evals/runs/<tag>/{results.jsonl, meta.json} (+ judge.jsonl if the judge ran).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import platform
import shutil
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from evals import judge as judge_mod
from evals.golden import GOLDEN_PATH, Scenario, load_golden
from evals.mock import MockAnswerer
from evals.pricing import DEFAULT_PRICES, PriceTable
from evals.provenance import (
    ROOT,
    corpus_files,
    corpus_manifest,
    git_info,
    golden_sha256,
)
from rag.entry import answer
from rag.pipeline import RagConfig, RagPipeline, build_pipeline

RUNS_DIR = Path(__file__).resolve().parent / "runs"


def select(
    scenarios: list[Scenario],
    subset: int | None,
    categories: set[str] | None,
    exclude: set[str],
) -> list[Scenario]:
    pool = [
        s
        for s in scenarios
        if (not categories or s.category in categories) and s.category not in exclude
    ]
    if not subset or subset >= len(pool):
        return pool
    # Stratified, deterministic: take scenarios round-robin across categories in golden order.
    by_cat: dict[str, list[Scenario]] = {}
    for s in pool:
        by_cat.setdefault(s.category, []).append(s)
    picked: list[Scenario] = []
    i = 0
    while len(picked) < subset:
        for lst in by_cat.values():
            if i < len(lst) and len(picked) < subset:
                picked.append(lst[i])
        i += 1
    order = {s.id: n for n, s in enumerate(pool)}
    return sorted(picked, key=lambda s: order[s.id])


def make_data_dir(corpus: str) -> tuple[Path, tempfile.TemporaryDirectory | None]:
    """`full` = data/ as is. `no-fixtures` = a temp copy without the planted-injection docs."""
    data = ROOT / "data"
    if corpus == "full":
        return data, None
    tmp = tempfile.TemporaryDirectory()
    out = Path(tmp.name)
    (out / "corpus").mkdir()
    for f in corpus_files(data, exclude_fixtures=True):
        shutil.copy(f, out / "corpus" / f.name)
    shutil.copy(data / "directory.json", out / "directory.json")
    return out, tmp


def build(generator: str, cfg: RagConfig) -> RagPipeline:
    if generator == "mock":
        cfg.generator = "extractive"
        pipe = build_pipeline(cfg)
        pipe.answerer = MockAnswerer()
        return pipe
    cfg.generator = generator
    return build_pipeline(cfg)


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--subset", type=int, default=None)
    ap.add_argument("--generator", default="extractive", choices=["extractive", "gemini", "mock"])
    ap.add_argument("--corpus", default="full", choices=["full", "no-fixtures"])
    ap.add_argument(
        "--store",
        default=None,
        choices=["chroma", "memory"],
        help="override the vector store (default: RagConfig default, i.e. chroma)",
    )
    ap.add_argument("--categories", default="")
    ap.add_argument("--exclude-categories", default="")
    ap.add_argument("--prices", default=str(DEFAULT_PRICES))
    ap.add_argument("--judge", action="store_true", help="run the LLM judge if credentials exist")
    ap.add_argument("--out-dir", default=str(RUNS_DIR))
    ap.add_argument("--golden", default=str(GOLDEN_PATH))
    args = ap.parse_args(argv)

    if args.generator == "gemini" and not judge_mod.credentials_available():
        print(
            "ERROR: --generator gemini needs GEMINI_API_KEY or Vertex credentials; none found. "
            "Nothing was run.",
            file=sys.stderr,
        )
        return 2

    scenarios = select(
        load_golden(Path(args.golden)),
        args.subset,
        set(filter(None, args.categories.split(","))) or None,
        set(filter(None, args.exclude_categories.split(","))),
    )
    prices = PriceTable.load(args.prices)

    data_dir, tmp = make_data_dir(args.corpus)
    try:
        cfg = RagConfig(data_dir=data_dir)
        if args.store:
            cfg.store = args.store
        pipe = build(args.generator, cfg)
        records = []
        for s in scenarios:
            try:
                res = answer(s.user_id, s.message, history=None, pipeline=pipe)
                rec = {"result": res.to_dict(), "error": None}
            except Exception as e:  # noqa: BLE001 - one failure must not abort the run
                rec = {"result": None, "error": f"{type(e).__name__}: {e}"}
            records.append(
                {
                    "scenario_id": s.id,
                    "category": s.category,
                    "user_id": s.user_id,
                    "message": s.message,
                    **rec,
                }
            )
        manifest = corpus_manifest(ROOT / "data", exclude_fixtures=args.corpus == "no-fixtures")
        model_used = getattr(pipe.answerer, "model", None)
        config_snapshot = {
            k: (str(v) if isinstance(v, Path) else v) for k, v in dataclasses.asdict(cfg).items()
        }
    finally:
        if tmp:
            tmp.cleanup()

    # --- optional judge (skipped, not faked, without credentials)
    judge_meta: dict = {"status": "not_requested", "validated": False}
    verdicts = []
    if args.judge:
        if not judge_mod.credentials_available():
            judge_meta = {"status": "skipped", "reason": "no credentials", "validated": False}
        else:
            from evals.metrics import build_context

            ctx = build_context(ROOT / "data", cfg.chunking)
            client = judge_mod.make_client()
            for s, rec in zip(scenarios, records, strict=True):
                if rec["result"] is None:
                    continue
                r = rec["result"]
                chunks = [ctx.chunk_text.get(x["chunk_id"], "") for x in r["retrieved"]]
                verdicts.append(judge_mod.judge_one(client, s, r["answer"], chunks, r["refused"]))
            judge_meta = {"status": "run", "model": judge_mod.JUDGE_MODEL, "validated": False}

    out = Path(args.out_dir) / args.tag
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    if verdicts:
        (out / "judge.jsonl").write_text(
            "\n".join(json.dumps(dataclasses.asdict(v)) for v in verdicts) + "\n", encoding="utf-8"
        )
    meta = {
        "tag": args.tag,
        "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "git": git_info(),
        "python": platform.python_version(),
        "n_scenarios": len(scenarios),
        "subset": args.subset,
        "generator": args.generator,
        "models": {
            "answerer": model_used,
            "embedder": cfg.embedder,
            "judge": judge_mod.JUDGE_MODEL if judge_meta["status"] == "run" else None,
        },
        "price_table_version": prices.version,
        "config": config_snapshot,
        "corpus": {"variant": args.corpus, **manifest},
        "golden_sha256": golden_sha256(Path(args.golden)),
        "golden_status": "provisional (pending human review of reference answers)",
        "judge": judge_meta,
        "excluded_categories": sorted(set(filter(None, args.exclude_categories.split(",")))),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    n_err = sum(r["error"] is not None for r in records)
    print(f"ran {len(records)} scenarios ({n_err} errors) -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
