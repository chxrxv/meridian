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
import os
import platform
import re
import shutil
import sys
import tempfile
import time
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


def _is_daily_quota(err: str | None) -> bool:
    return bool(err) and isinstance(
        judge_mod.classify_error(Exception(err)), judge_mod.QuotaExhausted
    )


def _answer_with_retries(pipe, s, *, generator: str, retries: int, sleep=time.sleep):
    """Call answer(); for Gemini, retry when the API call failed and the pipeline silently fell
    back to extractive text. Returns (result, extra_requests, attempts). Model-behaviour fallbacks
    (invalid citation markers) are NOT retried: they are part of what is being measured."""
    extra, attempts = 0, 1
    res = answer(s.user_id, s.message, history=None, pipeline=pipe)
    while (
        generator == "gemini"
        and res.answerer_fallback
        and attempts <= retries
        and not (res.answerer_error or "").startswith("invalid or missing citation")
        and not _is_daily_quota(res.answerer_error)
    ):
        extra += res.requests
        delay = min(60.0, 2.0 * 2 ** (attempts - 1))
        m = re.search(r"retry in ([0-9.]+)\s*s", (res.answerer_error or "").lower())
        sleep(min(120.0, float(m.group(1))) if m else delay)
        attempts += 1
        res = answer(s.user_id, s.message, history=None, pipeline=pipe)
    return res, extra, attempts


def main(argv: list[str] | None = None, *, sleep=time.sleep) -> int:
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
    ap.add_argument(
        "--min-interval",
        type=float,
        default=None,
        help="seconds between scenarios (gemini default: MERIDIAN_GEN_MIN_INTERVAL or 6.0)",
    )
    ap.add_argument("--retries", type=int, default=3, help="gemini: retries after an API failure")
    ap.add_argument("--out-dir", default=str(RUNS_DIR))
    ap.add_argument("--golden", default=str(GOLDEN_PATH))
    args = ap.parse_args(argv)

    if args.generator == "gemini" and not judge_mod.credentials_available():
        print(
            "ERROR: --generator gemini needs a free-tier GEMINI_API_KEY (Vertex AI is billed and "
            "is not used); none found. Nothing was run.",
            file=sys.stderr,
        )
        return 2
    interval = args.min_interval
    if interval is None:
        interval = (
            float(os.getenv("MERIDIAN_GEN_MIN_INTERVAL", "6.0"))
            if args.generator == "gemini"
            else 0.0
        )

    scenarios = select(
        load_golden(Path(args.golden)),
        args.subset,
        set(filter(None, args.categories.split(","))) or None,
        set(filter(None, args.exclude_categories.split(","))),
    )
    prices = PriceTable.load(args.prices)

    data_dir, tmp = make_data_dir(args.corpus)
    complete, stop_reason = True, None
    try:
        cfg = RagConfig(data_dir=data_dir)
        if args.store:
            cfg.store = args.store
        pipe = build(args.generator, cfg)
        records = []
        for n, s in enumerate(scenarios):
            if n and interval:
                sleep(interval)
            try:
                res, extra, attempts = _answer_with_retries(
                    pipe, s, generator=args.generator, retries=args.retries, sleep=sleep
                )
                rec = {
                    "result": res.to_dict(),
                    "error": None,
                    "extra_requests": extra,
                    "attempts": attempts,
                }
                if args.generator == "gemini" and _is_daily_quota(res.answerer_error):
                    complete, stop_reason = False, f"daily quota exhausted at {s.id}"
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
            if not complete:
                break
        manifest = corpus_manifest(ROOT / "data", exclude_fixtures=args.corpus == "no-fixtures")
        model_used = getattr(pipe.answerer, "model", None)
        config_snapshot = {
            k: (str(v) if isinstance(v, Path) else v) for k, v in dataclasses.asdict(cfg).items()
        }
    finally:
        if tmp:
            tmp.cleanup()

    out = Path(args.out_dir) / args.tag
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8"
    )
    fallbacks = sum(1 for r in records if r["result"] and r["result"].get("answerer_fallback"))
    meta = {
        "tag": args.tag,
        "started_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "git": git_info(),
        "python": platform.python_version(),
        "n_scenarios": len(scenarios),
        "n_run": len(records),
        "complete": complete,
        "stop_reason": stop_reason,
        "subset": args.subset,
        "generator": args.generator,
        "billing": "free tier (API key; no billing)" if model_used else "n/a (no model calls)",
        "models": {"answerer": model_used, "embedder": cfg.embedder, "judge": None},
        "answerer_fallbacks": fallbacks,
        "price_table_version": prices.version,
        "config": config_snapshot,
        "corpus": {"variant": args.corpus, **manifest},
        "golden_sha256": golden_sha256(Path(args.golden)),
        "golden_status": "provisional (pending human review of reference answers)",
        "judge": {"status": "not_run", "validated": False, "how": "python -m evals.judge_run"},
        "excluded_categories": sorted(set(filter(None, args.exclude_categories.split(",")))),
    }
    (out / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    n_err = sum(r["error"] is not None for r in records)
    print(f"ran {len(records)}/{len(scenarios)} scenarios ({n_err} errors) -> {out}")
    if not complete:
        print(
            f"STOPPED EARLY: {stop_reason}. Incomplete run: do not use as a baseline.",
            file=sys.stderr,
        )
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
