"""Stable entry point used by the eval harness (and later the agents/API).

`answer()` is a thin wrapper over `RagPipeline.ask`: it adds no logic of its own to retrieval,
thresholds or prompts. It only reshapes the result and adds model/token/latency metadata.

Limitation: the baseline pipeline has no conversation memory, so `history` is accepted for
interface stability and recorded in the result, but it does NOT influence retrieval or the answer.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from rag.pipeline import RagConfig, RagPipeline, build_pipeline

_default: RagPipeline | None = None


@dataclass
class RetrievedChunk:
    chunk_id: str
    doc_id: str
    dense: float
    lexical: float
    fused: float
    rerank: float


@dataclass
class AnswerResult:
    user_id: str
    message: str
    answer: str
    cited_chunk_ids: list[str]
    retrieved: list[RetrievedChunk]
    confidence: float
    refused: bool
    refusal_reason: str | None
    model: str | None  # None => extractive, no LLM involved
    tokens: dict[str, int] = field(default_factory=lambda: {"input": 0, "output": 0})
    latency_ms: float = 0.0
    acl_violations_blocked: int = 0
    history_used: bool = False  # always False in the baseline (see module docstring)

    def to_dict(self) -> dict:
        return asdict(self)


def default_pipeline() -> RagPipeline:
    global _default
    if _default is None:
        _default = build_pipeline(RagConfig())
    return _default


def answer(
    user_id: str,
    message: str,
    history: list[dict[str, str]] | None = None,
    *,
    pipeline: RagPipeline | None = None,
) -> AnswerResult:
    pipe = pipeline or default_pipeline()
    t0 = time.perf_counter()
    res = pipe.ask(user_id, message)
    latency = (time.perf_counter() - t0) * 1000

    usage = getattr(pipe.answerer, "last_usage", None) or {}
    return AnswerResult(
        user_id=user_id,
        message=message,
        answer=res.answer,
        cited_chunk_ids=[c.chunk_id for c in res.citations],
        retrieved=[
            RetrievedChunk(h.chunk.id, h.chunk.doc_id, h.dense, h.lexical, h.fused, h.rerank)
            for h in res.hits
        ],
        confidence=res.confidence,
        refused=res.refused,
        refusal_reason=res.refusal_reason,
        model=getattr(pipe.answerer, "model", None),
        tokens={"input": usage.get("input", 0), "output": usage.get("output", 0)},
        latency_ms=latency,
        acl_violations_blocked=res.acl_violations_blocked,
    )
