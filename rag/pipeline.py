"""End-to-end permission-aware RAG: retrieve -> confidence gate -> answer with citations."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from rag.acl import Directory
from rag.corpus import chunk_documents, load_documents
from rag.embeddings import Embedder, GeminiEmbedder, HashingEmbedder
from rag.generate import REFUSAL, Answerer, ExtractiveAnswerer, GeminiAnswerer
from rag.retriever import HybridRetriever, chunk_text_for_display
from rag.store import VectorStore, make_store
from rag.types import Citation, RagAnswer

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class RagConfig:
    """Tunable knobs. The defaults are the Phase-1 *baseline*; evals compare changes to these."""

    data_dir: Path = ROOT / "data"
    store: str = field(default_factory=lambda: os.getenv("MERIDIAN_STORE", "chroma"))
    embedder: str = field(default_factory=lambda: os.getenv("MERIDIAN_EMBEDDER", "hash"))
    generator: str = field(default_factory=lambda: os.getenv("MERIDIAN_GENERATOR", "extractive"))
    chunking: str = "fixed"  # fixed | section
    rerank: str = "lexical"  # lexical | none
    top_k: int = 4
    min_confidence: float = 0.35  # below this the pipeline refuses instead of answering
    persist_dir: str | None = None  # Chroma path; None = ephemeral


class RagPipeline:
    def __init__(
        self,
        directory: Directory,
        store: VectorStore,
        embedder: Embedder,
        answerer: Answerer,
        config: RagConfig,
    ) -> None:
        self.directory = directory
        self.config = config
        self.answerer = answerer
        self.retriever = HybridRetriever(
            directory, store, embedder, rerank=config.rerank, candidates=max(20, config.top_k * 4)
        )

    def ask(self, user_id: str, query: str) -> RagAnswer:
        t0 = time.perf_counter()
        res = self.retriever.retrieve(user_id, query, k=self.config.top_k)
        out = RagAnswer(
            query=query,
            user_id=user_id,
            answer="",
            hits=res.hits,
            confidence=res.confidence,
            acl_violations_blocked=res.acl_violations_blocked,
            timings_ms=dict(res.timings_ms),
        )
        # The refusal text is identical whether the answer is missing, weak, or restricted,
        # so a refusal cannot be used to probe for the existence of documents.
        if not res.hits or res.confidence < self.config.min_confidence:
            out.refused, out.refusal_reason, out.answer = True, "low_confidence", REFUSAL
        else:
            t1 = time.perf_counter()
            idf = self.retriever._bm25_for(self.directory.effective_principals(user_id)).idf
            out.answer = self.answerer.answer(query, res.hits, idf)
            out.timings_ms["generate"] = (time.perf_counter() - t1) * 1000
            if out.answer == REFUSAL:
                out.refused, out.refusal_reason = True, "no_grounded_answer"
            else:
                out.citations = [
                    Citation(
                        n=n,
                        doc_id=h.chunk.doc_id,
                        title=h.chunk.title,
                        heading=h.chunk.heading,
                        chunk_id=h.chunk.id,
                        snippet=chunk_text_for_display(h.chunk),
                    )
                    for n, h in enumerate(res.hits, start=1)
                ]
        out.timings_ms["total"] = (time.perf_counter() - t0) * 1000
        return out


def build_pipeline(config: RagConfig | None = None) -> RagPipeline:
    cfg = config or RagConfig()
    directory = Directory.load(cfg.data_dir / "directory.json")
    docs = load_documents(cfg.data_dir / "corpus")
    chunks = chunk_documents(docs, cfg.chunking)

    embedder: Embedder = GeminiEmbedder() if cfg.embedder == "gemini" else HashingEmbedder()
    store = make_store(cfg.store, cfg.persist_dir)
    store.add(chunks, embedder.embed([c.text for c in chunks]))

    answerer: Answerer = GeminiAnswerer() if cfg.generator == "gemini" else ExtractiveAnswerer()
    return RagPipeline(directory, store, embedder, answerer, cfg)
