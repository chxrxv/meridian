from __future__ import annotations

import itertools

import pytest

from rag.acl import Directory
from rag.corpus import load_documents
from rag.pipeline import ROOT, RagConfig, RagPipeline, build_pipeline

DATA = ROOT / "data"


@pytest.fixture(scope="session")
def directory() -> Directory:
    return Directory.load(DATA / "directory.json")


@pytest.fixture(scope="session")
def docs():
    return load_documents(DATA / "corpus")


_COMBOS = list(itertools.product(["memory", "chroma"], ["fixed", "section"]))
_cache: dict[tuple[str, str], RagPipeline] = {}


def pipeline_for(store: str, chunking: str) -> RagPipeline:
    key = (store, chunking)
    if key not in _cache:
        _cache[key] = build_pipeline(
            RagConfig(store=store, chunking=chunking, embedder="hash", generator="extractive")
        )
    return _cache[key]


@pytest.fixture(params=_COMBOS, ids=lambda c: f"{c[0]}-{c[1]}")
def pipeline(request) -> RagPipeline:
    return pipeline_for(*request.param)
