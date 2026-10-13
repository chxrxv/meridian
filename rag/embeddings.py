"""Embedders. The default is an offline, deterministic hashing embedder.

It captures lexical overlap (unigrams + bigrams), NOT deep semantics. That is a deliberate
baseline: it keeps the repo runnable with no credentials and makes ACL tests deterministic.
`GeminiEmbedder` gives real semantic vectors when credentials are available.
"""

from __future__ import annotations

import os
import zlib
from typing import Protocol

import numpy as np

from rag.text import tokenize


class Embedder(Protocol):
    dim: int

    def embed(self, texts: list[str]) -> np.ndarray: ...


class HashingEmbedder:
    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for row, text in enumerate(texts):
            toks = tokenize(text)
            feats = toks + [f"{a}_{b}" for a, b in zip(toks, toks[1:], strict=False)]
            for f in feats:
                h = zlib.crc32(f.encode())
                sign = 1.0 if (h >> 31) & 1 else -1.0
                out[row, h % self.dim] += sign
            norm = np.linalg.norm(out[row])
            if norm:
                out[row] /= norm
        return out


class GeminiEmbedder:
    """Gemini embeddings via google-genai (API key or Vertex ADC); not covered by offline tests."""

    def __init__(self, model: str | None = None, dim: int = 768) -> None:
        from google import genai  # lazy: optional dependency

        self._client = genai.Client()
        self._model = model or os.getenv("MERIDIAN_EMBEDDING_MODEL", "gemini-embedding-001")
        self.dim = dim

    def embed(self, texts: list[str]) -> np.ndarray:
        from google.genai import types

        vecs: list[list[float]] = []
        for i in range(0, len(texts), 100):
            res = self._client.models.embed_content(
                model=self._model,
                contents=texts[i : i + 100],
                config=types.EmbedContentConfig(output_dimensionality=self.dim),
            )
            vecs.extend(e.values for e in res.embeddings)
        arr = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return arr / np.where(norms == 0, 1, norms)
