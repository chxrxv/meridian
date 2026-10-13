"""Reranking and the confidence signal.

`coverage_score` is an IDF-weighted fraction of distinctive query terms found in the chunk,
plus small bonuses for adjacent-term-pair matches and title hits. It is computed only over
ACL-visible chunks, so the confidence signal cannot reveal restricted content.
"""

from __future__ import annotations

from rag.text import tokenize
from rag.types import Hit


def coverage_score(query: str, text: str, title: str, idf: dict[str, float]) -> float:
    q = tokenize(query)
    if not q:
        return 0.0
    body = tokenize(text)
    body_set, title_set = set(body), set(tokenize(title))
    weights = {t: idf.get(t, 1.0) for t in set(q)}
    total = sum(weights.values()) or 1.0
    hit = sum(w for t, w in weights.items() if t in body_set or t in title_set)
    cov = hit / total
    bigrams_q = set(zip(q, q[1:], strict=False))
    bigrams_b = set(zip(body, body[1:], strict=False))
    bonus = 0.15 * (len(bigrams_q & bigrams_b) / len(bigrams_q)) if bigrams_q else 0.0
    title_bonus = 0.1 * (len(title_set & set(q)) / max(len(title_set), 1))
    return min(1.0, 0.75 * cov + bonus + title_bonus)


class LexicalReranker:
    name = "lexical"

    def __init__(self, idf: dict[str, float]) -> None:
        self.idf = idf

    def rerank(self, query: str, hits: list[Hit]) -> list[Hit]:
        for h in hits:
            h.rerank = coverage_score(query, h.chunk.text, h.chunk.title, self.idf)
        return sorted(hits, key=lambda h: (-h.rerank, -h.fused))


class NoReranker:
    """Keeps fused order but still computes the coverage score used for confidence."""

    name = "none"

    def __init__(self, idf: dict[str, float]) -> None:
        self.idf = idf

    def rerank(self, query: str, hits: list[Hit]) -> list[Hit]:
        for h in hits:
            h.rerank = coverage_score(query, h.chunk.text, h.chunk.title, self.idf)
        return hits
