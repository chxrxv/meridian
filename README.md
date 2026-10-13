# Meridian

A portfolio project: a governed agent suite for a **fictional** logistics company, *Meridian Freight*,
built on Google's Agent Development Kit (ADK) and Gemini on Vertex AI. All data is synthetic.
Nothing in this repo has been deployed to Google Cloud unless a doc says so explicitly.

> Status: **Phase 1 of 7** (scaffold, synthetic corpus, ACL model, permission-aware RAG baseline).
> Later phases add evals, the ADK agents, MCP database server, multimodal, security/observability,
> API/UI/infra, and documented eval improvements.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
python -m scripts.generate_data                       # already committed; regenerates deterministically
python -m rag.cli --user frank --debug "What discount can a sales manager approve?"
python -m rag.cli --user judy  --debug "How do I reset the break-glass admin password?"   # refused / no leak
pytest
```

No credentials are needed for Phase 1: embeddings are an offline hashing embedder and answers are
extractive. Set `GEMINI_API_KEY` (or Vertex ADC) and `MERIDIAN_EMBEDDER=gemini`,
`MERIDIAN_GENERATOR=gemini` to use Gemini; those paths are implemented but not covered by the
offline test suite.

## Phase 1 design: permission-aware RAG

| Concern | Approach |
| --- | --- |
| Identity | `data/directory.json`: 13 synthetic users, 12 groups with implication (`finance-managers` ⇒ `finance`). Unknown users raise; there is no default identity. |
| ACL | Allow-only lists of `user:` / `group:` tokens in each document's frontmatter; every chunk inherits its document's ACL. |
| Enforcement | (1) ACL is a **pre-filter inside the store query** (Chroma `where` / in-memory mask); (2) BM25 is built **only over visible chunks**, so IDF and confidence signals cannot leak; (3) the retriever **re-checks every chunk** and drops/counts violations as defence in depth. |
| Retrieval | Dense (hashing embedder by default) + BM25, reciprocal-rank fusion, lexical reranker. |
| Answers | Extractive with `[n]` citations; refusal when top rerank confidence < threshold. Refusals are identical for "missing" and "restricted" so they are not an existence oracle. |
| Stores | Local Chroma (default) or in-memory; Vertex AI Vector Search / RAG Engine is a stub that raises `NotImplementedError`. |

The Phase 1 configuration is deliberately a **simple baseline** (fixed-size chunking, lexical
reranker, fixed threshold) so later phases can show measured improvements.

## Layout

`agents/ rag/ connectors/ mcp_server/ tools/ api/ ui/ infra/ evals/ tests/ docs/ scripts/ data/`
(only `rag/`, `scripts/`, `tests/`, `data/` are populated so far).
