"""Tiny CLI for the RAG baseline.

python -m rag.cli --user erin "what is the discount limit?" --debug
"""

from __future__ import annotations

import argparse

from dotenv import load_dotenv

from rag.pipeline import RagConfig, build_pipeline


def main() -> None:
    load_dotenv()
    p = argparse.ArgumentParser(description="Ask the permission-aware RAG pipeline")
    p.add_argument("query")
    p.add_argument("--user", required=True, help="directory user id, e.g. alice")
    p.add_argument("--store", default=None, choices=["chroma", "memory"])
    p.add_argument("--chunking", default="fixed", choices=["fixed", "section"])
    p.add_argument("--debug", action="store_true", help="print retrieval scores")
    args = p.parse_args()

    cfg = RagConfig(chunking=args.chunking)
    if args.store:
        cfg.store = args.store
    ans = build_pipeline(cfg).ask(args.user, args.query)

    print(ans.answer)
    for c in ans.citations:
        print(f"  [{c.n}] {c.title} ({c.doc_id}) - {c.heading or 'excerpt'}")
    if args.debug:
        timings = {k: round(v, 1) for k, v in ans.timings_ms.items()}
        print(
            f"\nconfidence={ans.confidence:.3f} refused={ans.refused} "
            f"blocked={ans.acl_violations_blocked} timings={timings}"
        )
        for h in ans.hits:
            print(
                f"  {h.chunk.id:32} rerank={h.rerank:.3f} dense={h.dense:.3f} lex={h.lexical:.2f}"
            )


if __name__ == "__main__":
    main()
