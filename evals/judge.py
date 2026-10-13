"""LLM-as-judge. Pinned model (different from the model under test), temperature 0, JSON output.

STATUS: UNVALIDATED. Until `evals/judge_labels.jsonl` has human labels and `evals.labels agreement`
has been run, every report marks judge metrics "unvalidated". The judge is skipped (not faked)
when no credentials are available.

Judge prompts live here in evals/, outside any retrieval corpus.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

from evals.golden import Scenario

# Pinned judge. Must differ from MERIDIAN_MODEL (default gemini-2.5-flash) used by the generator.
JUDGE_MODEL = os.getenv("MERIDIAN_JUDGE_MODEL", "gemini-2.5-pro")

FAITHFULNESS_RUBRIC = """You are grading an internal-assistant answer for FAITHFULNESS.
Faithful = every factual claim in the ANSWER is supported by the CONTEXT passages. Claims that are
true in general but absent from the CONTEXT count as unsupported. A refusal is faithful.
Respond with JSON only:
{"faithful": true|false, "unsupported_claims": [string], "reason": string}."""

CORRECTNESS_RUBRIC = """You are grading an internal-assistant answer for CORRECTNESS against a
REFERENCE. Correct = the ANSWER states the facts in the REFERENCE (wording may differ) and
contradicts none of them. If the REFERENCE describes a required behaviour (refuse, ask a clarifying
question, decline a write action) the answer is correct only if it shows that behaviour.
Respond with JSON only: {"correct": true|false, "reason": string}."""

CLARIFY_RUBRIC = """The user request was ambiguous. Grade the ANSWER as a CLARIFYING QUESTION.
Good = it asks a specific question that would disambiguate the request (naming plausible options),
does not guess, and does not dump unrelated policy text.
Respond with JSON only: {"good_clarifying_question": true|false, "reason": string}."""


@dataclass
class JudgeVerdict:
    scenario_id: str
    faithful: bool | None = None
    correct: bool | None = None
    good_clarifying_question: bool | None = None
    reasons: dict[str, str] | None = None
    error: str | None = None


def credentials_available() -> bool:
    return bool(
        os.getenv("GEMINI_API_KEY")
        or os.getenv("GOOGLE_API_KEY")
        or os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true"
    )


def _ask(client: Any, rubric: str, payload: str) -> dict[str, Any]:
    res = client.models.generate_content(
        model=JUDGE_MODEL,
        contents=f"{rubric}\n\n{payload}",
        config={"temperature": 0, "response_mime_type": "application/json"},
    )
    return json.loads(res.text)


def judge_one(
    client: Any, s: Scenario, answer: str, context_chunks: list[str], refused: bool
) -> JudgeVerdict:
    ctx = "\n\n".join(f"[{i}] {t}" for i, t in enumerate(context_chunks, 1)) or "(none)"
    v = JudgeVerdict(s.id, reasons={})
    try:
        f = _ask(client, FAITHFULNESS_RUBRIC, f"CONTEXT:\n{ctx}\n\nANSWER:\n{answer}")
        v.faithful, v.reasons["faithful"] = bool(f["faithful"]), str(f.get("reason", ""))
        c = _ask(
            client,
            CORRECTNESS_RUBRIC,
            f"QUESTION:\n{s.message}\n\nREFERENCE:\n{s.expected}\n\nANSWER:\n{answer}",
        )
        v.correct, v.reasons["correct"] = bool(c["correct"]), str(c.get("reason", ""))
        if s.category == "ambiguous":
            g = _ask(client, CLARIFY_RUBRIC, f"QUESTION:\n{s.message}\n\nANSWER:\n{answer}")
            v.good_clarifying_question = bool(g["good_clarifying_question"])
            v.reasons["clarify"] = str(g.get("reason", ""))
    except Exception as e:  # noqa: BLE001 - judge failures are recorded, never fatal
        v.error = f"{type(e).__name__}: {e}"
    return v


def make_client() -> Any:
    from google import genai  # lazy: optional dependency

    return genai.Client()
