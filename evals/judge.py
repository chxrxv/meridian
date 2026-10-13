"""LLM-as-judge for faithfulness, correctness and clarifying-question quality.

STATUS: UNVALIDATED and NOT YET RUN. Judge numbers stay "unvalidated" until the project owner's
blind human labels (`evals/judge_labels.jsonl`) are filled and `python -m evals.labels agreement`
has been computed. The judge is run only through `python -m evals.judge_run`, which refuses to
start without `--labels-confirmed` and a fully filled label file.

Design constraints (free tier only, no billing):
  * One request per scenario (faithfulness + correctness + optional clarification graded in a
    single JSON reply) to stay inside free-tier quotas.
  * Flash-class judge model, temperature 0, JSON output. The judge model must differ from the
    model under test (`check_independent`).
  * Client-side throttle (requests per minute), exponential backoff with jitter honouring a
    server-provided "retry in Ns", and an on-disk cache keyed by (backend, model, prompt) so a
    rerun costs zero requests.
  * Daily-quota exhaustion stops the run with partial results saved; it is never papered over.
  * Optional local Ollama fallback, used only if `MERIDIAN_JUDGE_OLLAMA_MODEL` is set. Verdicts
    record which backend produced them; mixing backends weakens comparability.
  * Vertex AI is refused: it is billed, and this project judges on a free-tier API key only.

Judge prompts live here in evals/, outside any retrieval corpus.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.golden import Scenario

CACHE_DIR = Path(__file__).resolve().parent / ".judge_cache"

RUBRIC = """You are grading an internal-assistant ANSWER. Reply with JSON only, in this shape:
{"faithful": true|false, "faithful_reason": string,
 "correct": true|false, "correct_reason": string,
 "good_clarifying_question": true|false|null, "clarify_reason": string}

1. faithful: every factual claim in the ANSWER is supported by the CONTEXT passages. Claims that are
   true in general but absent from the CONTEXT count as unsupported. A refusal is faithful.
2. correct: the ANSWER states the facts in the REFERENCE (wording may differ) and contradicts none
   of them. If the REFERENCE describes a required behaviour (refuse, ask a clarifying question,
   decline a write action, answer only from permitted documents) the answer is correct only if it
   shows that behaviour.
3. good_clarifying_question: only when the QUESTION is ambiguous (the REFERENCE says to ask a
   clarifying question); otherwise null. Good = asks a specific question naming plausible options,
   does not guess, and does not dump unrelated policy text."""


@dataclass
class JudgeConfig:
    backend: str = field(default_factory=lambda: os.getenv("MERIDIAN_JUDGE_BACKEND", "gemini"))
    model: str = field(
        default_factory=lambda: os.getenv("MERIDIAN_JUDGE_MODEL", "gemini-2.5-flash-lite")
    )
    rpm: float = field(default_factory=lambda: float(os.getenv("MERIDIAN_JUDGE_RPM", "10")))
    max_retries: int = 5
    base_delay: float = 2.0
    max_delay: float = 120.0
    ollama_model: str | None = field(
        default_factory=lambda: os.getenv("MERIDIAN_JUDGE_OLLAMA_MODEL") or None
    )
    ollama_host: str = field(
        default_factory=lambda: os.getenv("OLLAMA_HOST", "http://localhost:11434")
    )


# ----------------------------------------------------------------------------- errors


class QuotaExhausted(RuntimeError):
    """Daily quota exhausted (or retries exhausted) and no fallback is configured: stop the run."""


class TransientError(RuntimeError):
    """Rate limit / server error worth retrying. `retry_after` seconds if the server said so."""

    def __init__(self, msg: str, retry_after: float | None = None) -> None:
        super().__init__(msg)
        self.retry_after = retry_after


def classify_error(e: Exception) -> TransientError | QuotaExhausted | Exception:
    """Map an SDK/HTTP exception to retry / stop / fatal."""
    text = str(e)
    low = text.lower()
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    m = re.search(r"retry in ([0-9.]+)\s*s", low)
    retry_after = float(m.group(1)) if m else None
    if "perday" in low.replace(" ", "").replace("_", "") or "per day" in low or "daily" in low:
        return QuotaExhausted(f"daily quota exhausted: {text[:200]}")
    if code == 429 or "resource_exhausted" in low or "rate limit" in low or "429" in low:
        return TransientError(f"rate limited: {text[:200]}", retry_after)
    if (isinstance(code, int) and code >= 500) or any(
        s in low for s in ("503", "unavailable", "timed out", "timeout", "connection")
    ):
        return TransientError(f"server/network error: {text[:200]}", retry_after)
    return e


def credentials_available() -> bool:
    """Free-tier API key only. Vertex AI is billed, so it does not count (and is refused)."""
    if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true":
        return False
    return bool(os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"))


def check_independent(judge_model: str, model_under_test: str | None) -> None:
    if model_under_test is not None and judge_model == model_under_test:
        raise ValueError(
            f"judge model {judge_model!r} equals the model under test; pin a different judge "
            "(MERIDIAN_JUDGE_MODEL)"
        )


# ----------------------------------------------------------------------------- infrastructure


class Throttle:
    """Spaces calls at least 60/rpm seconds apart. Clock and sleep are injectable for tests."""

    def __init__(
        self,
        rpm: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.interval = 60.0 / rpm if rpm > 0 else 0.0
        self._clock, self._sleep, self._last = clock, sleep, None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            gap = self.interval - (now - self._last)
            if gap > 0:
                self._sleep(gap)
                now = self._clock()
        self._last = now


class DiskCache:
    def __init__(self, directory: Path | None = CACHE_DIR) -> None:
        self.dir = directory
        if directory:
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(backend: str, model: str, prompt: str) -> str:
        return hashlib.sha256(f"{backend}\n{model}\n{prompt}".encode()).hexdigest()

    def get(self, key: str) -> str | None:
        if not self.dir:
            return None
        p = self.dir / f"{key}.json"
        return p.read_text(encoding="utf-8") if p.exists() else None

    def put(self, key: str, text: str) -> None:
        if self.dir:
            (self.dir / f"{key}.json").write_text(text, encoding="utf-8")


# ----------------------------------------------------------------------------- backends


class GeminiBackend:
    name = "gemini"

    def __init__(self, model: str, client: Any | None = None) -> None:
        if client is None:
            from google import genai  # lazy: optional dependency

            client = genai.Client()  # API key from the environment
        self.client, self.model = client, model

    def complete(self, prompt: str) -> str:
        try:
            res = self.client.models.generate_content(
                model=self.model,
                contents=prompt,
                config={"temperature": 0, "response_mime_type": "application/json"},
            )
        except Exception as e:  # noqa: BLE001
            raise classify_error(e) from e
        return res.text


class OllamaBackend:
    name = "ollama"

    def __init__(self, model: str, host: str = "http://localhost:11434", opener=None) -> None:
        self.model, self.host = model, host.rstrip("/")
        self._open = opener or urllib.request.urlopen

    def complete(self, prompt: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
                "options": {"temperature": 0},
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.host}/api/chat", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with self._open(req, timeout=300) as r:
                return json.loads(r.read())["message"]["content"]
        except urllib.error.URLError as e:
            raise TransientError(f"ollama unreachable: {e}") from e


# ----------------------------------------------------------------------------- the judge


@dataclass
class JudgeVerdict:
    scenario_id: str
    faithful: bool | None = None
    correct: bool | None = None
    good_clarifying_question: bool | None = None
    reasons: dict[str, str] = field(default_factory=dict)
    backend: str | None = None
    model: str | None = None
    cached: bool = False
    error: str | None = None


def build_prompt(s: Scenario, answer: str, context_chunks: list[str]) -> str:
    ctx = "\n\n".join(f"[{i}] {t}" for i, t in enumerate(context_chunks, 1)) or "(none)"
    return (
        f"{RUBRIC}\n\nQUESTION:\n{s.message}\n\nREFERENCE:\n{s.expected}\n\n"
        f"CONTEXT:\n{ctx}\n\nANSWER:\n{answer}"
    )


def parse_reply(text: str) -> dict[str, Any]:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    data = json.loads(t)
    for k in ("faithful", "correct"):
        if not isinstance(data.get(k), bool):
            raise ValueError(f"judge reply missing boolean {k!r}")
    return data


class Judge:
    def __init__(
        self,
        cfg: JudgeConfig,
        primary: Any,
        *,
        fallback: Any | None = None,
        cache: DiskCache | None = None,
        throttle: Throttle | None = None,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.cfg, self.primary, self.fallback = cfg, primary, fallback
        self.cache = cache if cache is not None else DiskCache()
        self.throttle = throttle or Throttle(cfg.rpm)
        self._sleep, self._rng = sleep, rng or random.Random(0)
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0, "fallback_used": 0}

    def _call_with_backoff(self, backend: Any, prompt: str) -> str:
        for attempt in range(self.cfg.max_retries + 1):
            self.throttle.wait()
            self.stats["requests"] += 1
            try:
                return backend.complete(prompt)
            except TransientError as e:
                if attempt == self.cfg.max_retries:
                    raise QuotaExhausted(f"retries exhausted: {e}") from e
                delay = (
                    e.retry_after
                    if e.retry_after is not None
                    else min(self.cfg.max_delay, self.cfg.base_delay * 2**attempt)
                )
                delay = min(self.cfg.max_delay, delay) + self._rng.uniform(0, 0.5)
                self.stats["retries"] += 1
                self._sleep(delay)
        raise AssertionError("unreachable")

    def _complete(self, prompt: str) -> tuple[str, str, str, bool]:
        """Return (text, backend_name, model, cached)."""
        for backend, model in ((self.primary, self.cfg.model),) + (
            ((self.fallback, self.cfg.ollama_model or ""),) if self.fallback else ()
        ):
            key = DiskCache.key(backend.name, model, prompt)
            hit = self.cache.get(key)
            if hit is not None:
                self.stats["cache_hits"] += 1
                return hit, backend.name, model, True
            try:
                text = self._call_with_backoff(backend, prompt)
            except QuotaExhausted:
                if backend is self.primary and self.fallback is not None:
                    self.stats["fallback_used"] += 1
                    continue
                raise
            self.cache.put(key, text)
            return text, backend.name, model, False
        raise QuotaExhausted("no backend available")

    def judge_one(self, s: Scenario, answer: str, context_chunks: list[str]) -> JudgeVerdict:
        v = JudgeVerdict(s.id)
        text, v.backend, v.model, v.cached = self._complete(build_prompt(s, answer, context_chunks))
        try:
            d = parse_reply(text)
        except (ValueError, json.JSONDecodeError) as e:
            v.error = f"unparseable judge reply: {e}"
            return v
        v.faithful, v.correct = d["faithful"], d["correct"]
        gc = d.get("good_clarifying_question")
        v.good_clarifying_question = (
            gc if isinstance(gc, bool) and s.category == "ambiguous" else None
        )
        v.reasons = {
            "faithful": str(d.get("faithful_reason", "")),
            "correct": str(d.get("correct_reason", "")),
            "clarify": str(d.get("clarify_reason", "")),
        }
        return v


def make_judge(cfg: JudgeConfig | None = None) -> Judge:
    cfg = cfg or JudgeConfig()
    if cfg.backend == "gemini":
        if os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true":
            raise SystemExit(
                "refusing to judge via Vertex AI: it is billed; use a free-tier API key"
            )
        if not credentials_available():
            raise SystemExit("no GEMINI_API_KEY / GOOGLE_API_KEY found; judge not run")
        primary: Any = GeminiBackend(cfg.model)
    elif cfg.backend == "ollama":
        if not cfg.ollama_model:
            raise SystemExit("MERIDIAN_JUDGE_BACKEND=ollama needs MERIDIAN_JUDGE_OLLAMA_MODEL")
        cfg.model = cfg.ollama_model
        primary = OllamaBackend(cfg.ollama_model, cfg.ollama_host)
    else:
        raise SystemExit(f"unknown judge backend {cfg.backend!r}")
    fallback = (
        OllamaBackend(cfg.ollama_model, cfg.ollama_host)
        if cfg.backend == "gemini" and cfg.ollama_model
        else None
    )
    return Judge(cfg, primary, fallback=fallback)
