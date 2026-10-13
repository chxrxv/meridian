"""Core data types for the RAG layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Document:
    id: str
    title: str
    type: str
    owner: str
    ref: str
    allow: frozenset[str]
    text: str  # markdown body (frontmatter stripped)


@dataclass(frozen=True)
class Chunk:
    id: str
    doc_id: str
    title: str
    doc_type: str
    heading: str
    text: str
    allow: frozenset[str]
    ordinal: int


@dataclass
class Hit:
    """A chunk with the scores that led to it being selected (kept for trace/debug)."""

    chunk: Chunk
    dense: float = 0.0
    lexical: float = 0.0
    fused: float = 0.0
    rerank: float = 0.0


@dataclass(frozen=True)
class Citation:
    n: int
    doc_id: str
    title: str
    heading: str
    chunk_id: str
    snippet: str


@dataclass
class RagAnswer:
    query: str
    user_id: str
    answer: str
    citations: list[Citation] = field(default_factory=list)
    refused: bool = False
    refusal_reason: str | None = None
    confidence: float = 0.0
    hits: list[Hit] = field(default_factory=list)
    acl_violations_blocked: int = 0  # chunks the retriever guard dropped (should always be 0)
    timings_ms: dict[str, float] = field(default_factory=dict)
