"""Hybrid, permission-aware retrieval.

Order of operations (the ACL is applied before anything is scored):
  1. Resolve the caller's effective principals from the directory (unknown user => error).
  2. Dense search with the ACL as a store-side pre-filter.
  3. BM25 over only the chunks that principal set can see.
  4. Reciprocal-rank fusion, then rerank.
  5. Guard: re-verify every returned chunk against the principals; drop and count violations.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from rag.acl import Directory, can_access
from rag.embeddings import Embedder
from rag.lexical import BM25
from rag.rerank import LexicalReranker, NoReranker
from rag.store import VectorStore
from rag.types import Chunk, Hit

log = logging.getLogger("meridian.rag")


@dataclass
class RetrievalResult:
    hits: list[Hit]
    confidence: float
    acl_violations_blocked: int
    timings_ms: dict[str, float]


class HybridRetriever:
    def __init__(
        self,
        directory: Directory,
        store: VectorStore,
        embedder: Embedder,
        *,
        rerank: str = "lexical",
        candidates: int = 20,
        rrf_k: int = 60,
    ) -> None:
        self.directory = directory
        self.store = store
        self.embedder = embedder
        self.rerank_mode = rerank
        self.candidates = candidates
        self.rrf_k = rrf_k
        self._bm25_cache: dict[frozenset[str], BM25] = {}

    def _bm25_for(self, principals: frozenset[str]) -> BM25:
        if principals not in self._bm25_cache:
            self._bm25_cache[principals] = BM25(self.store.visible_chunks(principals))
        return self._bm25_cache[principals]

    def retrieve(self, user_id: str, query: str, k: int = 4) -> RetrievalResult:
        t0 = time.perf_counter()
        principals = self.directory.effective_principals(user_id)

        qvec = self.embedder.embed([query])[0]
        dense = self.store.dense_search(qvec, principals, self.candidates)
        t1 = time.perf_counter()

        bm25 = self._bm25_for(principals)
        lexical = bm25.search(query, self.candidates)
        t2 = time.perf_counter()

        hits: dict[str, Hit] = {}
        for rank, (chunk, score) in enumerate(dense):
            h = hits.setdefault(chunk.id, Hit(chunk))
            h.dense = score
            h.fused += 1.0 / (self.rrf_k + rank + 1)
        for rank, (chunk, score) in enumerate(lexical):
            h = hits.setdefault(chunk.id, Hit(chunk))
            h.lexical = score
            h.fused += 1.0 / (self.rrf_k + rank + 1)
        fused = sorted(hits.values(), key=lambda h: -h.fused)

        # Defence in depth: never trust the store to have filtered correctly.
        safe, blocked = [], 0
        for h in fused:
            if can_access(h.chunk.allow, principals):
                safe.append(h)
            else:
                blocked += 1
                log.error(
                    "ACL guard blocked chunk %s for user %s (store returned inaccessible data)",
                    h.chunk.id,
                    user_id,
                )

        rr_cls = LexicalReranker if self.rerank_mode == "lexical" else NoReranker
        reranker = rr_cls(bm25.idf)
        ranked = reranker.rerank(query, safe)
        top = ranked[:k]
        confidence = max((h.rerank for h in ranked), default=0.0)
        t3 = time.perf_counter()

        return RetrievalResult(
            hits=top,
            confidence=confidence,
            acl_violations_blocked=blocked,
            timings_ms={
                "dense": (t1 - t0) * 1000,
                "lexical": (t2 - t1) * 1000,
                "fuse_rerank": (t3 - t2) * 1000,
            },
        )


def chunk_text_for_display(chunk: Chunk, limit: int = 240) -> str:
    text = " ".join(chunk.text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
