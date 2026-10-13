"""A deterministic stand-in for the LLM answerer, used by the CI smoke eval (no credentials).

It is NOT a model of Gemini's behaviour: it exists to exercise the harness end to end
(token accounting, cost, citation markers). Its numbers say nothing about answer quality.
"""

from __future__ import annotations

from rag.generate import REFUSAL
from rag.text import sentences
from rag.types import Hit


class MockAnswerer:
    name = "mock"
    model = "mock-model"

    def __init__(self) -> None:
        self.last_usage: dict[str, int] | None = None

    def answer(self, query: str, hits: list[Hit], idf: dict[str, float]) -> str:
        body = " ".join(h.chunk.text for h in hits)
        self.last_usage = {"input": len(query.split()) + len(body.split()), "output": 12}
        first = sentences(hits[0].chunk.text)
        if not first:
            return REFUSAL
        return f"{first[0]} [1]"
