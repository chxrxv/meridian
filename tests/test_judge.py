"""Judge infrastructure with fakes only: no network, no credentials, no real sleeping."""

from __future__ import annotations

import json
import random
from pathlib import Path
from types import SimpleNamespace

import pytest

from evals import judge as J
from evals import judge_run
from evals.golden import load_golden

SCEN = {s.id: s for s in load_golden()}
GOOD = {
    "faithful": True,
    "faithful_reason": "supported",
    "correct": False,
    "correct_reason": "missing fact",
    "good_clarifying_question": None,
    "clarify_reason": "",
}


class FakeBackend:
    """Scripted backend: each item is a reply string or an exception to raise."""

    def __init__(self, script, name="gemini"):
        self.script, self.calls, self.name = list(script), [], name

    def complete(self, prompt):
        self.calls.append(prompt)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_judge(primary, *, fallback=None, cache=None, rpm=0, retries=3, ollama_model=None):
    sleeps: list[float] = []
    cfg = J.JudgeConfig(
        backend="gemini",
        model="judge-model",
        rpm=rpm,
        max_retries=retries,
        ollama_model=ollama_model,
    )
    j = J.Judge(
        cfg,
        primary,
        fallback=fallback,
        cache=cache or J.DiskCache(None),
        throttle=J.Throttle(rpm, clock=lambda: 0.0, sleep=sleeps.append),
        sleep=sleeps.append,
        rng=random.Random(0),
    )
    return j, sleeps


# ------------------------------------------------------------------ verdicts


def test_one_request_grades_all_dimensions_with_the_rubric():
    reply = json.dumps({**GOOD, "good_clarifying_question": True, "clarify_reason": "specific"})
    be = FakeBackend([reply])
    j, _ = make_judge(be)
    v = j.judge_one(SCEN["amb-01"], "Which one do you mean?", [])
    assert (v.faithful, v.correct, v.good_clarifying_question) == (True, False, True)
    assert v.backend == "gemini" and v.model == "judge-model" and not v.cached
    assert len(be.calls) == 1 and "REFERENCE" in be.calls[0] and "ANSWER" in be.calls[0]
    assert j.stats["requests"] == 1


def test_clarification_grade_is_ignored_outside_ambiguous_scenarios():
    be = FakeBackend([json.dumps({**GOOD, "good_clarifying_question": True})])
    v = make_judge(be)[0].judge_one(SCEN["single-01"], "20 days [1]", ["ctx"])
    assert v.good_clarifying_question is None


@pytest.mark.parametrize("bad", ["not json", '{"faithful": "yes", "correct": true}', "{}"])
def test_unparseable_replies_are_recorded_not_fatal(bad):
    v = make_judge(FakeBackend([bad]))[0].judge_one(SCEN["single-01"], "a", [])
    assert v.error and v.faithful is None


def test_code_fenced_json_is_accepted():
    reply = "```json\n" + json.dumps(GOOD) + "\n```"
    assert make_judge(FakeBackend([reply]))[0].judge_one(SCEN["single-01"], "a", []).faithful


# ------------------------------------------------------------------ throttle / backoff / cache


def test_throttle_spaces_calls_by_the_requests_per_minute_budget():
    now, sleeps = [0.0], []
    t = J.Throttle(
        10, clock=lambda: now[0], sleep=lambda d: (sleeps.append(d), now.__setitem__(0, now[0] + d))
    )
    t.wait()  # first call: no wait
    t.wait()  # immediately again: must wait the full 6s
    now[0] += 2.0
    t.wait()  # 2s elapsed of 6s: wait 4s
    assert sleeps == [pytest.approx(6.0), pytest.approx(4.0)]


def test_backoff_retries_transient_errors_and_honours_retry_after():
    be = FakeBackend(
        [J.TransientError("429", retry_after=12.0), J.TransientError("503"), json.dumps(GOOD)]
    )
    j, sleeps = make_judge(be)
    v = j.judge_one(SCEN["single-01"], "a", [])
    assert v.faithful is True and j.stats["retries"] == 2 and j.stats["requests"] == 3
    assert 12.0 <= sleeps[0] < 12.6  # server said 12s (+ small jitter)
    assert 4.0 <= sleeps[1] < 4.6  # base 2s * 2^1 for the second attempt


def test_gives_up_with_quota_exhausted_after_max_retries():
    be = FakeBackend([J.TransientError("429")] * 3)
    j, _ = make_judge(be, retries=2)
    with pytest.raises(J.QuotaExhausted):
        j.judge_one(SCEN["single-01"], "a", [])
    assert j.stats["requests"] == 3


def test_disk_cache_makes_reruns_free(tmp_path):
    cache = J.DiskCache(tmp_path)
    be = FakeBackend([json.dumps(GOOD)])
    j1, _ = make_judge(be, cache=cache)
    v1 = j1.judge_one(SCEN["single-01"], "same answer", ["ctx"])
    j2, _ = make_judge(FakeBackend([]), cache=cache)  # would crash if it made a request
    v2 = j2.judge_one(SCEN["single-01"], "same answer", ["ctx"])
    assert not v1.cached and v2.cached and j2.stats["requests"] == 0 and j2.stats["cache_hits"] == 1
    j3, _ = make_judge(FakeBackend([json.dumps(GOOD)]), cache=cache)
    assert not j3.judge_one(SCEN["single-01"], "different answer", ["ctx"]).cached


def test_cache_key_includes_backend_and_model():
    assert J.DiskCache.key("gemini", "a", "p") != J.DiskCache.key("gemini", "b", "p")
    assert J.DiskCache.key("gemini", "a", "p") != J.DiskCache.key("ollama", "a", "p")


# ------------------------------------------------------------------ error classification


@pytest.mark.parametrize(
    "msg,code,kind",
    [
        ("429 RESOURCE_EXHAUSTED. Please retry in 36.4s", 429, J.TransientError),
        (
            "Quota exceeded for metric ... GenerateRequestsPerDayPerProjectPerModel-FreeTier",
            429,
            J.QuotaExhausted,
        ),
        ("503 UNAVAILABLE model overloaded", 503, J.TransientError),
        ("The read operation timed out", None, J.TransientError),
        ("400 invalid argument", 400, Exception),
    ],
)
def test_classify_error(msg, code, kind):
    e = Exception(msg)
    e.code = code
    out = J.classify_error(e)
    assert isinstance(out, kind)
    if "36.4" in msg:
        assert out.retry_after == pytest.approx(36.4)


def test_gemini_backend_requests_temperature_zero_json_and_maps_errors():
    seen = {}

    class Models:
        def generate_content(self, model, contents, config):
            seen.update(model=model, config=config)
            return SimpleNamespace(text="{}")

    be = J.GeminiBackend("m1", client=SimpleNamespace(models=Models()))
    be.complete("p")
    assert seen == {
        "model": "m1",
        "config": {"temperature": 0, "response_mime_type": "application/json"},
    }

    class Boom:
        def generate_content(self, **kw):
            raise Exception("429 rate limit")

    with pytest.raises(J.TransientError):
        J.GeminiBackend("m1", client=SimpleNamespace(models=Boom())).complete("p")


# ------------------------------------------------------------------ fallback / guards


def test_ollama_fallback_is_used_only_when_primary_is_exhausted_and_is_recorded():
    primary = FakeBackend([J.TransientError("429")] * 2, name="gemini")
    local = FakeBackend([json.dumps(GOOD)], name="ollama")
    j, _ = make_judge(primary, fallback=local, retries=1, ollama_model="llama-local")
    v = j.judge_one(SCEN["single-01"], "a", [])
    assert v.backend == "ollama" and v.model == "llama-local" and j.stats["fallback_used"] == 1


def test_no_fallback_configured_means_the_run_stops():
    j, _ = make_judge(FakeBackend([J.QuotaExhausted("daily")]))
    with pytest.raises(J.QuotaExhausted):
        j.judge_one(SCEN["single-01"], "a", [])


def test_ollama_backend_posts_json_mode_at_temperature_zero():
    captured = {}

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"message": {"content": "{}"}}).encode()

    def opener(req, timeout):
        captured["url"], captured["body"] = req.full_url, json.loads(req.data)
        return Resp()

    assert J.OllamaBackend("llama-local", "http://h:1/", opener=opener).complete("p") == "{}"
    assert captured["url"] == "http://h:1/api/chat"
    assert captured["body"]["format"] == "json" and captured["body"]["options"] == {
        "temperature": 0
    }


def test_judge_must_differ_from_model_under_test():
    J.check_independent("gemini-2.5-flash-lite", "gemini-2.5-flash")
    J.check_independent("anything", None)  # extractive: no model under test
    with pytest.raises(ValueError):
        J.check_independent("gemini-2.5-flash", "gemini-2.5-flash")


def test_default_judge_is_flash_class_and_differs_from_default_generator():
    assert "flash" in J.JudgeConfig().model and J.JudgeConfig().model != "gemini-2.5-flash"


def test_vertex_is_refused_and_does_not_count_as_credentials(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert not J.credentials_available()
    with pytest.raises(SystemExit) as e:
        J.make_judge(J.JudgeConfig(backend="gemini"))
    assert "billed" in str(e.value)


def test_no_key_means_no_judge(monkeypatch):
    for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI"):
        monkeypatch.delenv(k, raising=False)
    assert not J.credentials_available()
    with pytest.raises(SystemExit):
        J.make_judge(J.JudgeConfig(backend="gemini"))


def test_ollama_primary_needs_a_model():
    with pytest.raises(SystemExit):
        J.make_judge(J.JudgeConfig(backend="ollama", ollama_model=None))


# ------------------------------------------------------------------ the human-label gate


def _labels(path: Path, filled: bool, n: int = 10):
    rows = [
        {
            "scenario_id": f"s{i}",
            "human_faithful": True if filled else None,
            "human_correct": False if filled else None,
        }
        for i in range(n)
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")


def test_judge_run_refuses_without_the_confirmation_flag(tmp_path, capsys):
    labels = tmp_path / "l.jsonl"
    _labels(labels, filled=True)
    assert judge_run.main(["x", "--labels", str(labels), "--runs-dir", str(tmp_path)]) == 3
    assert "labels-confirmed" in capsys.readouterr().err


def test_judge_run_refuses_while_labels_are_unfilled_even_with_the_flag(tmp_path, capsys):
    labels = tmp_path / "l.jsonl"
    _labels(labels, filled=False)
    rc = judge_run.main(
        ["x", "--labels-confirmed", "--labels", str(labels), "--runs-dir", str(tmp_path)]
    )
    assert rc == 3 and "unlabelled" in capsys.readouterr().err
    short = tmp_path / "short.jsonl"
    _labels(short, filled=True, n=3)
    assert judge_run.main(["x", "--labels-confirmed", "--labels", str(short)]) == 3


def test_shipped_label_file_is_unfilled_so_the_judge_cannot_run_yet():
    ok, why = judge_run.labels_complete()
    assert not ok and "unlabelled" in why


def test_judge_run_end_to_end_with_fake_judge_resumes_and_stops_on_quota(tmp_path, monkeypatch):
    from evals.run import main as run_main

    run_main(
        [
            "--subset",
            "6",
            "--tag",
            "r",
            "--generator",
            "mock",
            "--store",
            "memory",
            "--out-dir",
            str(tmp_path),
        ]
    )
    labels = tmp_path / "l.jsonl"
    _labels(labels, filled=True)
    reply = json.dumps(GOOD)
    # 3 good replies then the daily quota runs out
    be = FakeBackend([reply, reply, reply, J.QuotaExhausted("daily quota")])
    j, _ = make_judge(be)
    args = ["r", "--labels-confirmed", "--labels", str(labels), "--runs-dir", str(tmp_path)]
    assert judge_run.main(args, judge=j) == 4  # incomplete
    out = tmp_path / "r"
    assert len((out / "judge.jsonl").read_text().splitlines()) == 3
    meta = json.loads((out / "judge_meta.json").read_text())
    assert meta["status"].startswith("incomplete") and meta["validated"] is False
    assert meta["billing"] == "free tier (API key only)"

    # resume: only the remaining 3 scenarios are judged
    j2, _ = make_judge(FakeBackend([reply, reply, reply]))
    assert judge_run.main(args, judge=j2) == 0
    assert len((out / "judge.jsonl").read_text().splitlines()) == 6
    assert json.loads((out / "judge_meta.json").read_text())["status"] == "complete"


def test_judge_run_rejects_a_judge_equal_to_the_model_under_test(tmp_path):
    from evals.run import main as run_main

    run_main(
        [
            "--subset",
            "6",
            "--tag",
            "r",
            "--generator",
            "mock",
            "--store",
            "memory",
            "--out-dir",
            str(tmp_path),
        ]
    )
    labels = tmp_path / "l.jsonl"
    _labels(labels, filled=True)
    j, _ = make_judge(FakeBackend([]))
    j.cfg.model = "mock-model"  # the mock generator's model name
    with pytest.raises(ValueError):
        judge_run.main(
            ["r", "--labels-confirmed", "--labels", str(labels), "--runs-dir", str(tmp_path)],
            judge=j,
        )
