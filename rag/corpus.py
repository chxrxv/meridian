"""Load documents from disk and split them into chunks."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from rag.types import Chunk, Document

_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def load_documents(corpus_dir: str | Path) -> list[Document]:
    docs = []
    for path in sorted(Path(corpus_dir).glob("*.md")):
        raw = path.read_text(encoding="utf-8")
        m = _FRONT.match(raw)
        if not m:
            raise ValueError(f"{path.name}: missing frontmatter")
        meta = yaml.safe_load(m.group(1))
        docs.append(
            Document(
                id=meta["id"],
                title=meta["title"],
                type=meta["type"],
                owner=meta.get("owner", ""),
                ref=meta.get("ref", ""),
                allow=frozenset(meta.get("allow") or []),
                text=raw[m.end() :].strip(),
            )
        )
    return docs


def _sections(doc: Document) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    heading, buf = "Overview", []
    for line in doc.text.splitlines():
        if line.startswith("## "):
            if buf and "".join(buf).strip():
                out.append((heading, "\n".join(buf).strip()))
            heading, buf = line[3:].strip(), []
        elif line.startswith("# "):
            continue
        else:
            buf.append(line)
    if buf and "".join(buf).strip():
        out.append((heading, "\n".join(buf).strip()))
    return out


def chunk_fixed(doc: Document, size: int = 60, overlap: int = 10) -> list[Chunk]:
    """Baseline: sliding word window over the whole body, ignoring section structure."""
    words = " ".join(doc.text.split()).split(" ")
    chunks, start, i = [], 0, 0
    while start < len(words):
        piece = " ".join(words[start : start + size])
        chunks.append(Chunk(f"{doc.id}#{i}", doc.id, doc.title, doc.type, "", piece, doc.allow, i))
        i += 1
        if start + size >= len(words):
            break
        start += size - overlap
    return chunks


def chunk_sections(doc: Document) -> list[Chunk]:
    """Section-aware: one chunk per heading, prefixed with 'Title > Heading' context."""
    chunks = []
    for i, (heading, body) in enumerate(_sections(doc)):
        text = f"{doc.title} > {heading}\n{body}"
        chunks.append(
            Chunk(f"{doc.id}#{i}", doc.id, doc.title, doc.type, heading, text, doc.allow, i)
        )
    return chunks


def chunk_documents(docs: list[Document], strategy: str = "fixed") -> list[Chunk]:
    fn = {"fixed": chunk_fixed, "section": chunk_sections}[strategy]
    return [c for d in docs for c in fn(d)]
