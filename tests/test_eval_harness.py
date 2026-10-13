"""Harness plumbing: pricing, selection, provenance, determinism, judge (fake client), and the
credential-free smoke eval used in CI (mocked model)."""

from __future__ import annotations

import json

import pytest

from evals import labels
from evals.determinism import behaviour_diff, diff_runs
from evals.golden import load_golden
from evals.pricing import PriceTable
from evals.provenance import ROOT, corpus_manifest, golden_sha256
from evals.report import load_run
from evals.report import main as report_main
from evals.run import main as run_main
from evals.run import select

SCENARIOS = load_golden()

# ------------------------------------------------------------------ pricing


def test_price_table_never_guesses(tmp_path):
    p = tmp_path / "p.json"
    p.write_text(
        json.dumps(
            {
                "version": "test-1",
                "per_million_tokens": {
                    "m": {"input": 1.0, "output": 2.0},
                    "unpriced": {"input": None, "output": None},
                },
            }
        )
    )
    t = PriceTable.load(p)
    assert t.version == "test-1"
    assert t.cost(None, 0, 0) == 0.0  # no model call => free
    assert t.cost("m", 1_000_000, 500_000) == pytest.approx(2.0)
    assert t.cost("unpriced", 10, 10) is None
    assert t.cost("unknown-model", 10, 10) is None


def test_shipped_price_table_is_empty():
    t = PriceTable.load()
    assert t.per_million == {} and t.version == "unset"
    assert t.cost("gemini-2.5-flash", 1000, 1000) is None


# ------------------------------------------------------------------ selection


def test_select_is_stratified_deterministic_and_filterable():
    a = select(SCENARIOS, 6, None, set())
    assert a == select(SCENARIOS, 6, None, set())
    assert len({s.category for s in a}) == 6
    assert [s.id for s in a] == [s.id for s in SCENARIOS if s in a]  # golden order preserved
    assert len(select(SCENARIOS, None, None, set())) == 60
    ex = select(SCENARIOS, None, None, {"prompt_injection"})
    assert len(ex) == 54 and all(s.category != "prompt_injection" for s in ex)
    only = select(SCENARIOS, None, {"pii"}, set())
    assert len(only) == 4


# ------------------------------------------------------------------ provenance


def test_manifest_hash_is_stable_and_distinguishes_fixture_exclusion():
    data = ROOT / "data"
    full, again = corpus_manifest(data), corpus_manifest(data)
    no_fx = corpus_manifest(data, exclude_fixtures=True)
    assert full == again
    assert full["n_docs"] == 52 and no_fx["n_docs"] == 49
    assert full["sha256"] != no_fx["sha256"]
    assert sorted(full["fixtures_included"]) == sorted(no_fx["fixtures_excluded"])
    assert len(full["fixtures_included"]) == 3


def test_golden_hash_ignores_line_ending_style(tmp_path):
    lf, crlf = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    lf.write_bytes(b'{"a": 1}\n{"b": 2}\n')
    crlf.write_bytes(b'{"a": 1}\r\n{"b": 2}\r\n')
    assert golden_sha256(lf) == golden_sha256(crlf)


# ------------------------------------------------------------------ determinism helper


def _rec(**result):
    base = {
        "answer": "a",
        "latency_ms": 1.0,
        "confidence": 0.5,
        "retrieved": [],
        "cited_chunk_ids": [],
        "refused": False,
    }
    return {"scenario_id": "x", "result": {**base, **result}, "error": None}


def test_diff_runs_ignores_latency_and_float_noise_but_not_real_changes():
    base = _rec()
    assert diff_runs([base], [_rec(latency_ms=9.0)]) == []
    noisy = _rec(confidence=0.5 + 2e-16)
    assert diff_runs([base], [noisy]) == []  # tolerant by default
    assert diff_runs([base], [noisy], digits=None) == ["x"]  # exact comparison sees it
    assert diff_runs([base], [_rec(answer="b")]) == ["x"]
    assert diff_runs([base], [_rec(confidence=0.51)]) == ["x"]
    assert diff_runs([base], [{**base, "scenario_id": "y"}]) == ["scenario id lists differ"]


def test_behaviour_diff_looks_only_at_ids_citations_refusal_and_answer():
    a = _rec(retrieved=[{"chunk_id": "d#0", "rerank": 0.1}], cited_chunk_ids=["d#0"])
    b = _rec(retrieved=[{"chunk_id": "d#0", "rerank": 0.9}], cited_chunk_ids=["d#0"])
    c = _rec(retrieved=[{"chunk_id": "d#1", "rerank": 0.1}], cited_chunk_ids=["d#0"])
    assert behaviour_diff([a], [b]) == []  # score changed, behaviour same
    assert behaviour_diff([a], [c]) == ["x"]


# ------------------------------------------------------------------ label agreement


def test_label_agreement_only_counts_labelled_and_judged(tmp_path, monkeypatch):
    lp = tmp_path / "labels.jsonl"
    lp.write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"scenario_id": "a", "human_faithful": True, "human_correct": True},
                {"scenario_id": "b", "human_faithful": False, "human_correct": None},
                {"scenario_id": "c", "human_faithful": True, "human_correct": False},
            ]
        )
    )
    monkeypatch.setattr(labels, "LABELS", lp)
    run = tmp_path / "t"
    run.mkdir()
    (run / "judge.jsonl").write_text(
        "\n".join(
            json.dumps(x)
            for x in [
                {"scenario_id": "a", "faithful": True, "correct": False},
                {"scenario_id": "b", "faithful": False, "correct": True},
            ]
        )
    )
    out = labels.agreement("t", tmp_path)
    assert out["faithful"] == {"n": 2, "agreement": 1.0}
    assert out["correct"] == {
        "n": 1,
        "agreement": 0.0,
    }  # b has no human correct label; c not judged


def test_agreement_without_judge_run_stops(tmp_path, monkeypatch):
    lp = tmp_path / "l.jsonl"
    lp.write_text("{}\n")
    monkeypatch.setattr(labels, "LABELS", lp)
    with pytest.raises(SystemExit):
        labels.agreement("nope", tmp_path)


# ------------------------------------------------------------------ CI smoke eval (mocked model)


def test_gemini_generator_without_credentials_runs_nothing(tmp_path, monkeypatch, capsys):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr("evals.run.load_dotenv", lambda: None)
    rc = run_main(["--tag", "x", "--generator", "gemini", "--out-dir", str(tmp_path)])
    assert rc == 2 and not list(tmp_path.iterdir())
    assert "Nothing was run" in capsys.readouterr().err


def test_smoke_eval_end_to_end_with_mock_model_and_report(tmp_path, capsys):
    args = ["--subset", "6", "--generator", "mock", "--store", "memory", "--out-dir", str(tmp_path)]
    assert run_main([*args, "--tag", "smoke-a"]) == 0
    assert run_main([*args, "--tag", "smoke-b"]) == 0

    a = load_run("smoke-a", tmp_path)
    b = load_run("smoke-b", tmp_path)
    assert len(a.records) == 6 and all(r["error"] is None for r in a.records)
    assert diff_runs(a.records, b.records) == []  # deterministic with the in-memory store
    assert a.meta["config"]["store"] == "memory"
    m = a.meta
    assert m["models"]["answerer"] == "mock-model" and m["generator"] == "mock"
    assert m["corpus"]["n_docs"] == 52 and len(m["corpus"]["sha256"]) == 64
    assert m["golden_sha256"] == golden_sha256(ROOT / "evals" / "golden.jsonl")
    assert m["price_table_version"] == "unset" and m["config"]["chunking"] == "fixed"
    assert "commit" in m["git"] and m["judge"]["validated"] is False
    assert m["judge"]["status"] == "not_run" and m["complete"] is True
    assert m["billing"] == "free tier (API key; no billing)"  # a model (mock) was involved
    assert all(r["result"]["tokens"]["input"] > 0 or r["result"]["refused"] for r in a.records)

    capsys.readouterr()
    assert report_main(["smoke-a", "--runs-dir", str(tmp_path), "--compare", "smoke-b"]) == 0
    out = capsys.readouterr().out
    assert "Permission leaks" in out and "UNVALIDATED" in out and "Five worst scenarios" in out
    assert "Comparison" in out and "+0.0 pp" in out


def test_no_fixtures_corpus_run_uses_49_docs(tmp_path):
    rc = run_main(
        [
            "--tag",
            "nf",
            "--generator",
            "mock",
            "--corpus",
            "no-fixtures",
            "--subset",
            "6",
            "--exclude-categories",
            "prompt_injection",
            "--out-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
    meta = load_run("nf", tmp_path).meta
    assert meta["corpus"]["n_docs"] == 49 and meta["corpus"]["variant"] == "no-fixtures"
    assert meta["excluded_categories"] == ["prompt_injection"]
    recs = load_run("nf", tmp_path).records
    assert all(r["category"] != "prompt_injection" for r in recs)
    assert not any(
        c.startswith(("rb-shared-drive-faq", "fin-supplier-onboarding", "sop-fleet-maintenance"))
        for r in recs
        if r["result"]
        for c in r["result"]["cited_chunk_ids"]
    )
