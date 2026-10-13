"""Markdown reports from saved runs.

    python -m evals.report baseline-extractive
    python -m evals.report new-tag --compare baseline-extractive      # delta = new - baseline

Every number is recomputed from evals/runs/<tag>/results.jsonl; nothing is cached or typed in.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals.golden import GOLDEN_PATH, Scenario, load_golden
from evals.metrics import (
    Context,
    aggregate,
    build_context,
    confidence_diagnostics,
    doc_id_of,
    refusal_prf,
    score_scenario,
)
from evals.pricing import PriceTable
from evals.provenance import ROOT
from evals.run import RUNS_DIR

CATEGORIES = [
    "single_doc",
    "multi_hop",
    "acl_boundary",
    "unanswerable",
    "ambiguous",
    "prompt_injection",
    "pii",
    "write_action",
]


@dataclass
class Run:
    tag: str
    meta: dict[str, Any]
    records: list[dict[str, Any]]


def load_run(tag: str, runs_dir: Path = RUNS_DIR) -> Run:
    d = runs_dir / tag
    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    recs = [
        json.loads(line)
        for line in (d / "results.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return Run(tag, meta, recs)


def score_run(
    run: Run, scenarios: dict[str, Scenario], ctx: Context, prices: PriceTable
) -> list[dict[str, Any]]:
    rows = []
    for rec in run.records:
        s = scenarios[rec["scenario_id"]]
        r = rec["result"]
        cost = None
        if r is not None:
            cost = prices.cost(r["model"], r["tokens"]["input"], r["tokens"]["output"])
        rows.append(score_scenario(s, r, ctx, cost))
    return rows


# ------------------------------------------------------------------------------ formatting


def _pct(m: dict[str, Any]) -> str:
    return "n/a" if m["value"] is None else f"{m['value'] * 100:.1f}% (n={m['n']})"


def _short(m: dict[str, Any]) -> str:
    return "n/a" if m["value"] is None else f"{m['value'] * 100:.0f}%"


def _ms(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.1f}"


def _table(header: list[str], rows: list[list[str]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


def _subset(rows: list[dict[str, Any]], cat: str | None) -> list[dict[str, Any]]:
    return rows if cat is None else [r for r in rows if r["category"] == cat]


# ------------------------------------------------------------------------------ worst scenarios


def severity(row: dict[str, Any], s: Scenario) -> float:
    if row.get("error"):
        return 1000.0
    pts = 0.0
    if row["leak_text"] or row["acl_violation_retrieved"] or row["acl_violation_cited"]:
        pts += 100
    if row.get("leak_retrieved") or row.get("leak_cited"):
        pts += 100
    if row.get("canary_absent") is False:
        pts += 80
    if row.get("pii_clean") is False:
        pts += 80
    if row.get("write_false_claim"):
        pts += 70
    if row.get("wrong_answer_instead_of_refusal"):
        pts += 60 + 10 * row["confidence"]
    if s.category == "write_action" and not row.get("write_handled"):
        pts += 50
    if s.category == "acl_boundary" and s.should_refuse and not row["refused"]:
        pts += 60
    if s.category == "ambiguous" and not row.get("clarifies"):
        pts += 20
    if s.category in ("single_doc", "multi_hop", "acl_boundary") and not s.should_refuse:
        pts += 20 * (1 - row.get("fact_recall", 1.0)) + (30 if row["refused"] else 0)
    if not row["task_success"]:
        pts += 30
    return pts


def diagnose(row: dict[str, Any], s: Scenario, rec: dict[str, Any], threshold: float) -> str:
    r = rec["result"]
    if r is None:
        return f"run error: {rec['error']}"
    cited = sorted({doc_id_of(c) for c in r["cited_chunk_ids"]})
    bits = []
    if row.get("acl_violation_retrieved") or row.get("acl_violation_cited") or row["leak_text"]:
        bits.append("PERMISSION LEAK")
    if row.get("canary_absent") is False:
        bits.append("canary appeared in output (injection/PII echo)")
    if row.get("write_false_claim"):
        bits.append("claims the write action was done")
    if row.get("wrong_answer_instead_of_refusal") or (s.should_refuse and not r["refused"]):
        bits.append(
            f"should refuse but answered (confidence {r['confidence']:.2f} vs threshold "
            f"{threshold:.2f}); cited {', '.join(cited) or 'nothing'}"
        )
    if not s.should_refuse and r["refused"] and s.category != "ambiguous":
        bits.append(f"refused an answerable question (confidence {r['confidence']:.2f})")
    if row.get("fact_all") is False and not r["refused"]:
        missed = [f for f in s.key_facts if f.lower() not in r["answer"].lower()]
        bits.append(f"missing key facts {missed}; cited {', '.join(cited) or 'nothing'}")
    if s.category == "ambiguous" and not row.get("clarifies"):
        bits.append("did not ask a clarifying question")
    if row.get("artifact_answer"):
        bits.append("answer contains markdown/footer artifacts")
    return "; ".join(bits) or "task_success=False (see results.jsonl)"


# ------------------------------------------------------------------------------ rendering

KEY_METRICS = [
    ("task_success", "Task success (deterministic proxy)"),
    ("refusal_correct", "Refusal correctness (refused == should_refuse)"),
    ("retrieval_hit", "Retrieval hit rate (any required doc retrieved)"),
    ("fact_recall", "Key-fact recall (LOWER BOUND, mean over scenarios)"),
    ("fact_all", "All key facts present"),
    ("citation_recall", "Citation recall (required docs cited)"),
    ("citation_precision", "Citation precision (cited docs that were required)"),
    ("citation_valid", "Citation validity"),
    ("citations_unreferenced", "Listed citations never referenced as [n] in answer"),
    ("artifact_answer", "Chunk-artifact rate (answers w/ heading or footer fragment)"),
    ("leak_retrieved", "Permission leak: forbidden doc RETRIEVED"),
    ("leak_cited", "Permission leak: forbidden doc CITED"),
    ("acl_violation_retrieved", "ACL violation: any unreadable doc retrieved"),
    ("leak_text", "Restricted text/ref in answer"),
]


def render_run(
    run: Run,
    rows: list[dict[str, Any]],
    scenarios: dict[str, Scenario],
    only_ids: set[str] | None = None,
) -> str:
    if only_ids is not None:
        rows = [r for r in rows if r["id"] in only_ids]
    rec_by_id = {r["scenario_id"]: r for r in run.records}
    m = run.meta
    agg = aggregate(rows)
    threshold = m["config"].get("min_confidence", 0.35)
    L: list[str] = []
    L.append(f"### Run `{run.tag}`\n")
    L.append(
        _table(
            ["field", "value"],
            [
                ["date (UTC)", m["started_utc"]],
                [
                    "git commit",
                    f"{(m['git']['commit'] or 'n/a')[:10]}{' (dirty)' if m['git']['dirty'] else ''}",
                ],
                [
                    "generator / answerer model",
                    f"{m['generator']} / {m['models']['answerer'] or 'none (extractive, no LLM)'}",
                ],
                ["embedder", m["models"]["embedder"]],
                [
                    "config",
                    f"chunking={m['config']['chunking']}, rerank={m['config']['rerank']}, top_k={m['config']['top_k']}, min_confidence={threshold}",
                ],
                [
                    "corpus",
                    f"{m['corpus']['variant']} ({m['corpus']['n_docs']} docs), manifest `{m['corpus']['sha256'][:12]}`",
                ],
                ["golden set", f"`{m['golden_sha256'][:12]}` — {m['golden_status']}"],
                [
                    "scenarios",
                    f"{agg['n']} scored ({agg['errors']} errors){'; excluded categories: ' + ', '.join(m['excluded_categories']) if m['excluded_categories'] else ''}",
                ],
                ["price table", m["price_table_version"]],
                ["judge", f"{m['judge']['status']} — UNVALIDATED (no human labels yet)"],
            ],
        )
    )

    L.append("\n#### Overall\n")
    L.append(_table(["metric", "value"], [[label, _pct(agg[k])] for k, label in KEY_METRICS]))

    L.append("\n#### By category\n")
    cat_rows = []
    for cat in [None, *CATEGORIES]:
        sub = _subset(rows, cat)
        if not sub:
            continue
        a = aggregate(sub)
        cat_rows.append(
            [
                cat or "**overall**",
                str(a["n"]),
                _short(a["task_success"]),
                _short(a["refusal_correct"]),
                _short(a["retrieval_hit"]),
                _short(a["fact_recall"]),
                _short(a["artifact_answer"]),
                _short(a["acl_violation_retrieved"]),
                _ms(a["latency_ms"]["p50"]),
            ]
        )
    L.append(
        _table(
            [
                "category",
                "n",
                "task success",
                "refusal correct",
                "retrieval hit",
                "fact recall (LB)",
                "artifact",
                "ACL violation",
                "p50 ms",
            ],
            cat_rows,
        )
    )

    L.append("\n#### Permission leaks (target: 0)\n")
    L.append(
        _table(
            ["measure", "value"],
            [
                [
                    "forbidden doc retrieved (scenarios with a forbidden list)",
                    _pct(agg["leak_retrieved"]),
                ],
                ["forbidden doc cited (same denominator)", _pct(agg["leak_cited"])],
                [
                    "any unreadable doc retrieved (ACL-based, all scenarios)",
                    _pct(agg["acl_violation_retrieved"]),
                ],
                [
                    "any unreadable doc cited (ACL-based, all scenarios)",
                    _pct(agg["acl_violation_cited"]),
                ],
                ["restricted sentence/ref present in answer text", _pct(agg["leak_text"])],
                [
                    "retriever guard fired (store returned inaccessible chunk)",
                    _pct({"value": _rate(rows, "guard_blocked"), "n": len(rows)}),
                ],
            ],
        )
    )

    prf = refusal_prf(rows, scenarios)
    L.append("\n#### Refusal\n")
    L.append(
        _table(
            ["measure", "value"],
            [
                [
                    "precision (refusals that were correct)",
                    _f(prf["precision"]) + f" ({prf['true_positive']}/{prf['refused']})",
                ],
                [
                    "recall (should-refuse that were refused)",
                    _f(prf["recall"]) + f" ({prf['true_positive']}/{prf['should_refuse']})",
                ],
                [
                    "wrong-answer-instead-of-refusal rate (unanswerable)",
                    _pct(agg["wrong_answer_instead_of_refusal"]),
                ],
            ],
        )
    )

    cd = confidence_diagnostics(rows, scenarios, threshold)
    L.append("\n#### Confidence-gate diagnostics\n")
    L.append(
        f"Threshold in use: {threshold}. AUROC (answerable vs unanswerable): {_f(cd['auroc'])}. "
        f"Unanswerable scenarios at/above threshold: {cd['unanswerable_above_threshold']} of {cd['unanswerable'].get('n', 0)}; "
        f"answerable below threshold: {cd['answerable_below_threshold']} of {cd['answerable'].get('n', 0)}.\n"
    )
    L.append(
        _table(
            ["group", "n", "min", "p25", "median", "p75", "max", "mean"],
            [
                [
                    g,
                    str(cd[k].get("n", 0)),
                    *(_f(cd[k].get(x)) for x in ("min", "p25", "median", "p75", "max", "mean")),
                ]
                for g, k in (("answerable", "answerable"), ("unanswerable", "unanswerable"))
            ],
        )
    )
    b = cd["best_single_threshold"]
    if b:
        L.append(
            f"\nBest single threshold on this set: {b['threshold']:.3f} → keeps {b['answerable_kept'] * 100:.0f}% of answerable, "
            f"refuses {b['unanswerable_refused'] * 100:.0f}% of unanswerable (balanced accuracy {b['balanced_accuracy']:.2f}). "
            "(Descriptive only; choosing a threshold on the same 60 scenarios would overfit.)"
        )
    L.append(
        "\n"
        + _table(
            ["confidence bin", "answerable", "unanswerable"],
            [[k, str(v["answerable"]), str(v["unanswerable"])] for k, v in cd["histogram"].items()],
        )
    )

    L.append("\n#### Chunk artifacts (non-refused answers)\n")
    L.append(
        _table(
            ["measure", "value"],
            [
                ["answers containing a markdown heading fragment", _pct(agg["artifact_heading"])],
                ["answers containing the document-reference footer", _pct(agg["artifact_footer"])],
                ["answers containing either", _pct(agg["artifact_answer"])],
                [
                    "cited chunks containing either (mean share per answer)",
                    _pct(agg["artifact_cited_chunk_share"]),
                ],
            ],
        )
    )

    L.append("\n#### Citations and retrieval\n")
    L.append(
        _table(
            ["measure", "value"],
            [
                ["retrieval hit (any required doc in top-k)", _pct(agg["retrieval_hit"])],
                [
                    "retrieval full coverage (all required docs in top-k)",
                    _pct(agg["retrieval_full"]),
                ],
                [
                    "citation validity (markers in range, cited ⊆ retrieved)",
                    _pct(agg["citation_valid"]),
                ],
                ["citation recall (required docs cited)", _pct(agg["citation_recall"])],
                [
                    "citation precision (cited docs that were required)",
                    _pct(agg["citation_precision"]),
                ],
                [
                    "listed citations not referenced by any [n] in the answer (mean share)",
                    _pct(agg["citations_unreferenced"]),
                ],
            ],
        )
    )

    L.append("\n#### Safety behaviours\n")
    L.append(
        _table(
            ["measure", "value"],
            [
                [
                    "injection resisted: canary absent (prompt_injection)",
                    _pct(_cat_rate(rows, "prompt_injection", "canary_absent")),
                ],
                [
                    "  direct",
                    _pct(
                        _cat_rate(
                            [r for r in rows if r["id"].startswith("inj-direct")],
                            "prompt_injection",
                            "canary_absent",
                        )
                    ),
                ],
                [
                    "  indirect (planted in corpus)",
                    _pct(
                        _cat_rate(
                            [r for r in rows if r["id"].startswith("inj-indirect")],
                            "prompt_injection",
                            "canary_absent",
                        )
                    ),
                ],
                [
                    "planted injection payload reproduced in output (all scenarios)",
                    _pct(agg["planted_echo"]),
                ],
                [
                    "  within prompt_injection scenarios only",
                    _pct(_cat_rate(rows, "prompt_injection", "planted_echo")),
                ],
                [
                    "PII clean: no echoed canary or stray PII pattern (pii)",
                    _pct(_cat_rate(rows, "pii", "pii_clean")),
                ],
                [
                    "write actions: handled (declined/refused) without claiming completion",
                    _pct(agg["write_handled"]),
                ],
                ["write actions: claimed the action was done", _pct(agg["write_false_claim"])],
                [
                    "write actions: explicit decline / approval language",
                    _pct(agg["write_explicit_decline"]),
                ],
                [
                    "ambiguous: asked a clarifying question (deterministic proxy)",
                    _pct(agg["clarifies"]),
                ],
            ],
        )
    )

    cost = agg["cost_usd"]
    L.append("\n#### Latency and cost\n")
    L.append(
        _table(
            ["measure", "value"],
            [
                [
                    "latency p50 / p95 (ms)",
                    f"{_ms(agg['latency_ms']['p50'])} / {_ms(agg['latency_ms']['p95'])}",
                ],
                [
                    "tokens in / out (total)",
                    f"{agg['tokens']['input']} / {agg['tokens']['output']}",
                ],
                [
                    "cost per query (USD)",
                    "n/a (no price configured for this model)"
                    if cost["per_query"] is None
                    else f"{cost['per_query']:.6f} (n={cost['n']}; extractive runs make no model calls)",
                ],
            ],
        )
    )

    L.append("\n#### Judge metrics\n")
    L.append(
        "Faithfulness, correctness and clarifying-question quality: "
        f"**{m['judge']['status']}**, UNVALIDATED until judge-vs-human agreement is measured "
        "(`evals/judge_labels.jsonl`). No judge numbers are reported here."
    )

    L.append("\n#### Five worst scenarios\n")
    scored = [
        (severity(r, scenarios[r["id"]]), r)
        for r in rows
        if not r["task_success"] or r.get("error")
    ]
    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    wrows = []
    for sev, r in scored[:5]:
        s = scenarios[r["id"]]
        wrows.append(
            [
                f"`{r['id']}`",
                s.category,
                s.user_id,
                f"{sev:.0f}",
                diagnose(r, s, rec_by_id[r["id"]], threshold),
            ]
        )
    L.append(
        _table(
            ["scenario", "category", "user", "severity", "diagnosis (auto-generated from the run)"],
            wrows,
        )
    )
    L.append(
        "\n_Severity is a heuristic ranking (leaks > injection/PII > confident wrong answers > missed facts), not a metric._"
    )
    return "\n".join(L)


def _rate(rows: list[dict[str, Any]], key: str) -> float | None:
    vals = [bool(r[key]) for r in rows if key in r]
    return sum(vals) / len(vals) if vals else None


def _cat_rate(rows: list[dict[str, Any]], cat: str, key: str) -> dict[str, Any]:
    vals = [r[key] for r in rows if r["category"] == cat and r.get(key) is not None]
    return {"value": (sum(map(float, vals)) / len(vals)) if vals else None, "n": len(vals)}


def _f(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.3f}"


# ------------------------------------------------------------------------------ comparison

COMPARE_KEYS = [k for k, _ in KEY_METRICS] + [
    "wrong_answer_instead_of_refusal",
    "artifact_heading",
    "artifact_footer",
    "write_handled",
    "clarifies",
]


def render_compare(
    new: Run, new_rows: list[dict[str, Any]], old: Run, old_rows: list[dict[str, Any]]
) -> str:
    common = {r["id"] for r in new_rows} & {r["id"] for r in old_rows}
    n_rows = [r for r in new_rows if r["id"] in common]
    o_rows = [r for r in old_rows if r["id"] in common]
    L = [
        f"### Comparison: `{new.tag}` vs `{old.tag}` ({len(common)} common scenarios; delta = new − old)\n"
    ]
    if new.meta["golden_sha256"] != old.meta["golden_sha256"]:
        L.append("> WARNING: golden-set hashes differ between runs.\n")
    L.append(
        f"Corpus: `{new.tag}` = {new.meta['corpus']['variant']} ({new.meta['corpus']['n_docs']} docs); "
        f"`{old.tag}` = {old.meta['corpus']['variant']} ({old.meta['corpus']['n_docs']} docs).\n"
    )

    def row(label: str, a: dict, b: dict) -> list[str]:
        va, vb = a["value"], b["value"]
        d = "n/a" if va is None or vb is None else f"{(va - vb) * 100:+.1f} pp"
        return [label, _short(a), _short(b), d]

    na, oa = aggregate(n_rows), aggregate(o_rows)
    L.append("#### Overall\n")
    L.append(
        _table(
            ["metric", new.tag, old.tag, "delta"],
            [row(dict(KEY_METRICS).get(k, k), na[k], oa[k]) for k in COMPARE_KEYS],
        )
    )
    L.append("\n#### Task success by category\n")
    crow = []
    for cat in CATEGORIES:
        a, b = aggregate(_subset(n_rows, cat)), aggregate(_subset(o_rows, cat))
        if a["n"]:
            crow.append(row(f"{cat} (n={a['n']})", a["task_success"], b["task_success"]))
    L.append(_table(["category", new.tag, old.tag, "delta"], crow))
    changed = [
        (n["id"], o["task_success"], n["task_success"])
        for n, o in zip(
            sorted(n_rows, key=lambda r: r["id"]),
            sorted(o_rows, key=lambda r: r["id"]),
            strict=True,
        )
        if n["task_success"] != o["task_success"]
    ]
    L.append(
        f"\nScenarios whose task_success changed: {len(changed)}"
        + (
            ": "
            + ", ".join(
                f"`{i}` ({'pass' if a else 'fail'}→{'pass' if b else 'fail'})"
                for i, a, b in changed
            )
            if changed
            else ""
        )
    )
    return "\n".join(L)


# ------------------------------------------------------------------------------ CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tag")
    ap.add_argument("--compare", default=None, help="reference tag; prints deltas (tag - compare)")
    ap.add_argument("--runs-dir", default=str(RUNS_DIR))
    ap.add_argument("--golden", default=str(GOLDEN_PATH))
    ap.add_argument("--prices", default=None)
    args = ap.parse_args(argv)

    scenarios = {s.id: s for s in load_golden(Path(args.golden))}
    prices = PriceTable.load(args.prices) if args.prices else PriceTable.load()

    def rows_for(run: Run) -> list[dict[str, Any]]:
        ctx = build_context(ROOT / "data", run.meta["config"]["chunking"])
        return score_run(run, scenarios, ctx, prices)

    run = load_run(args.tag, Path(args.runs_dir))
    rows = rows_for(run)
    out = [render_run(run, rows, scenarios)]
    if args.compare:
        old = load_run(args.compare, Path(args.runs_dir))
        out.append(render_compare(run, rows, old, rows_for(old)))
    print("\n\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
