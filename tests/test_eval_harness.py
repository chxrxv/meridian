"""Harness plumbing: pricing, selection, provenance, determinism, judge (fake client), and the
credential-free smoke eval used in CI (mocked model)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from evals import judge, labels
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


def test_shipped_price_table_has_no_invented_prices():
    t = PriceTable.load()
    assert all(v["input"] is None and v["output"] is None for v in t.per_million.values())


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


# ------------------------------------------------------------------ judge with a fake client


class FakeJudgeClient:
    def __init__(self, replies, raise_on=None):
        self.replies, self.calls, self.raise_on = list(replies), [], raise_on
        self.models = SimpleNamespace(generate_content=self._gen)

    def _gen(self, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self.raise_on is not None and len(self.calls) == self.raise_on:
            raise RuntimeError("rate limited")
        return SimpleNamespace(text=json.dumps(self.replies.pop(0)))


def test_judge_uses_pinned_model_temperature_zero_and_json():
    s = next(x for x in SCENARIOS if x.id == "single-01")
    fake = FakeJudgeClient(
        [{"faithful": True, "reason": "ok"}, {"correct": False, "reason": "wrong"}]
    )
    v = judge.judge_one(
        fake, s, "20 days [1]", ["Full-time employees accrue 20 days"], refused=False
    )
    assert (v.faithful, v.correct, v.error) == (True, False, None)
    assert v.good_clarifying_question is None
    for c in fake.calls:
        assert c["model"] == judge.JUDGE_MODEL
        assert c["config"] == {"temperature": 0, "response_mime_type": "application/json"}
    assert "CONTEXT" in fake.calls[0]["contents"] and "REFERENCE" in fake.calls[1]["contents"]


def test_judge_grades_clarifying_questions_only_for_ambiguous_and_records_errors():
    amb = next(x for x in SCENARIOS if x.category == "ambiguous")
    fake = FakeJudgeClient(
        [{"faithful": True}, {"correct": True}, {"good_clarifying_question": True}]
    )
    v = judge.judge_one(fake, amb, "Which policy?", [], refused=False)
    assert v.good_clarifying_question is True and len(fake.calls) == 3
    failing = FakeJudgeClient([{"faithful": True}], raise_on=2)
    v2 = judge.judge_one(failing, amb, "x", [], refused=False)
    assert v2.error and "rate limited" in v2.error and v2.faithful is True


def test_judge_model_differs_from_generator_default():
    assert judge.JUDGE_MODEL != "gemini-2.5-flash"


def test_credentials_detection(monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(k, raising=False)
    assert not judge.credentials_available()
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert judge.credentials_available()


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
    assert run_main([*args, "--tag", "smoke-b", "--judge"]) == 0  # no creds in CI => skipped

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
    assert b.meta["judge"]["status"] == "skipped" or b.meta["judge"]["status"] == "run"
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
