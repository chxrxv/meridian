"""Pluggable vector stores.

ACL filtering happens *inside* the store query (pre-filter), so inaccessible chunks are never
scored, ranked or returned. The retriever re-checks every result as defence in depth
(see rag.retriever).
"""

from __future__ import annotations

import contextlib
import json
from typing import Protocol

import numpy as np

from rag.types import Chunk


class VectorStore(Protocol):
    def add(self, chunks: list[Chunk], vectors: np.ndarray) -> None: ...

    def dense_search(
        self, query_vec: np.ndarray, principals: frozenset[str], k: int
    ) -> list[tuple[Chunk, float]]: ...

    def visible_chunks(self, principals: frozenset[str]) -> list[Chunk]: ...

    def count(self) -> int: ...


def _allowed(chunk: Chunk, principals: frozenset[str]) -> bool:
    return bool(chunk.allow & principals)


class InMemoryStore:
    def __init__(self) -> None:
        self._chunks: list[Chunk] = []
        self._vecs = np.zeros((0, 0), dtype=np.float32)

    def add(self, chunks: list[Chunk], vectors: np.ndarray) -> None:
        self._chunks.extend(chunks)
        self._vecs = vectors if self._vecs.size == 0 else np.vstack([self._vecs, vectors])

    def dense_search(self, query_vec, principals, k):
        idx = [i for i, c in enumerate(self._chunks) if _allowed(c, principals)]
        if not idx:
            return []
        sims = self._vecs[idx] @ query_vec
        order = np.argsort(-sims)[:k]
        return [(self._chunks[idx[i]], float(sims[i])) for i in order]

    def visible_chunks(self, principals):
        return [c for c in self._chunks if _allowed(c, principals)]

    def count(self) -> int:
        return len(self._chunks)


def _acl_key(token: str) -> str:
    return "acl_" + token.replace(":", "__")


class ChromaStore:
    """Local Chroma (persistent when a path is given, ephemeral otherwise).

    Each allowed principal becomes a boolean metadata flag, so the ACL filter is a native
    `where` clause that Chroma evaluates before ranking.
    """

    def __init__(self, path: str | None = None, collection: str | None = None) -> None:
        import uuid

        import chromadb

        # Ephemeral clients share process-wide state, so persistent stores use a stable name
        # and ephemeral ones a unique one (several pipelines can coexist, e.g. in tests).
        collection = collection or ("meridian" if path else f"meridian-{uuid.uuid4().hex[:8]}")

        self._client = chromadb.PersistentClient(path=path) if path else chromadb.EphemeralClient()
        # rebuild on every start; ingestion is cheap and this avoids stale ACLs
        with contextlib.suppress(Exception):  # collection may not exist yet
            self._client.delete_collection(collection)
        self._col = self._client.create_collection(collection, metadata={"hnsw:space": "cosine"})

    @staticmethod
    def _where(principals: frozenset[str]) -> dict:
        clauses = [{_acl_key(p): True} for p in sorted(principals)]
        return clauses[0] if len(clauses) == 1 else {"$or": clauses}

    @staticmethod
    def _to_chunk(id_: str, text: str, meta: dict) -> Chunk:
        return Chunk(
            id=id_,
            doc_id=meta["doc_id"],
            title=meta["title"],
            doc_type=meta["doc_type"],
            heading=meta["heading"],
            text=text,
            allow=frozenset(json.loads(meta["allow"])),
            ordinal=meta["ordinal"],
        )

    def add(self, chunks, vectors):
        metas = []
        for c in chunks:
            m = {
                "doc_id": c.doc_id,
                "title": c.title,
                "doc_type": c.doc_type,
                "heading": c.heading,
                "ordinal": c.ordinal,
                "allow": json.dumps(sorted(c.allow)),
            }
            m.update({_acl_key(p): True for p in c.allow})
            metas.append(m)
        self._col.add(
            ids=[c.id for c in chunks],
            documents=[c.text for c in chunks],
            embeddings=vectors.tolist(),
            metadatas=metas,
        )

    def dense_search(self, query_vec, principals, k):
        n = self._col.count()
        if n == 0:
            return []
        res = self._col.query(
            query_embeddings=[query_vec.tolist()],
            n_results=min(k, n),
            where=self._where(principals),
        )
        out = []
        for id_, doc, meta, dist in zip(
            res["ids"][0],
            res["documents"][0],
            res["metadatas"][0],
            res["distances"][0],
            strict=True,
        ):
            out.append((self._to_chunk(id_, doc, meta), 1.0 - float(dist)))
        return out

    def visible_chunks(self, principals):
        res = self._col.get(where=self._where(principals), include=["documents", "metadatas"])
        return [
            self._to_chunk(i, d, m)
            for i, d, m in zip(res["ids"], res["documents"], res["metadatas"], strict=True)
        ]

    def count(self) -> int:
        return self._col.count()


def make_store(kind: str, path: str | None = None) -> VectorStore:
    if kind == "memory":
        return InMemoryStore()
    if kind == "chroma":
        return ChromaStore(path)
    if kind == "vertex":
        raise NotImplementedError(
            "Vertex AI Vector Search / RAG Engine adapter is described in docs/ARCHITECTURE.md "
            "but is not implemented in this build."
        )
    raise ValueError(f"unknown store: {kind}")
