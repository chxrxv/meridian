"""BM25 over an ACL-scoped chunk set."""

from __future__ import annotations

import math
from collections import Counter

from rag.text import tokenize
from rag.types import Chunk


class BM25:
    """Built only from chunks the caller may see, so IDF statistics cannot leak other content."""

    def __init__(self, chunks: list[Chunk], k1: float = 1.5, b: float = 0.75) -> None:
        self.chunks = chunks
        self.k1, self.b = k1, b
        self.docs = [Counter(tokenize(c.text)) for c in chunks]
        self.lens = [sum(d.values()) for d in self.docs]
        self.avg = (sum(self.lens) / len(self.lens)) if self.lens else 0.0
        df: Counter[str] = Counter()
        for d in self.docs:
            df.update(d.keys())
        n = len(chunks)
        self.idf = {t: math.log(1 + (n - f + 0.5) / (f + 0.5)) for t, f in df.items()}

    def search(self, query: str, k: int) -> list[tuple[Chunk, float]]:
        q = tokenize(query)
        scored = []
        for chunk, tf, ln in zip(self.chunks, self.docs, self.lens, strict=True):
            s = 0.0
            for t in q:
                f = tf.get(t, 0)
                if f:
                    s += (
                        self.idf.get(t, 0.0)
                        * f
                        * (self.k1 + 1)
                        / (f + self.k1 * (1 - self.b + self.b * ln / (self.avg or 1)))
                    )
            if s > 0:
                scored.append((chunk, s))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]
