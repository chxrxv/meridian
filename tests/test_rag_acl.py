"""ACL enforcement at retrieval time. The key invariant: a user never receives a chunk (or a
string derived from one) from a document they cannot access, for any query."""

from __future__ import annotations

import re

import pytest

from rag.acl import can_access
from rag.generate import REFUSAL
from rag.retriever import HybridRetriever
from rag.text import sentences
from tests.conftest import pipeline_for

PROBE_KINDS = ("title", "ref", "body")


def _probes(doc) -> list[str]:
    body = re.sub(r"\s+", " ", doc.text.split("## ", 1)[-1])
    first = sentences(body)[1] if len(sentences(body)) > 1 else body
    return [doc.title, f"document reference {doc.ref}", first]


def test_no_user_ever_retrieves_an_inaccessible_chunk(pipeline, directory, docs):
    """Exhaustive: every user x every document they cannot read x three probe queries."""
    checked = 0
    for user_id in directory.users:
        principals = directory.effective_principals(user_id)
        for doc in docs:
            if can_access(doc.allow, principals):
                continue
            for q in _probes(doc):
                ans = pipeline.ask(user_id, q)
                checked += 1
                assert ans.acl_violations_blocked == 0
                leaked = [h.chunk.id for h in ans.hits if h.chunk.doc_id == doc.id]
                assert not leaked, f"{user_id} got {leaked} via {q!r}"
                assert not any(c.doc_id == doc.id for c in ans.citations)
                assert doc.ref not in ans.answer
    assert checked > 1000


def test_answer_text_never_contains_restricted_sentences(pipeline, directory, docs):
    for user_id in ("judy", "alice", "erin", "ivan"):
        principals = directory.effective_principals(user_id)
        readable = " ".join(
            re.sub(r"\s+", " ", d.text) for d in docs if can_access(d.allow, principals)
        )
        for doc in docs:
            if can_access(doc.allow, principals):
                continue
            secret = sentences(re.sub(r"\s+", " ", doc.text.split("## ", 1)[-1]))[1]
            if secret in readable:
                continue  # identical wording exists in a readable doc (e.g. similar contracts)
            ans = pipeline.ask(user_id, secret)
            assert secret not in ans.answer


@pytest.mark.parametrize(
    "user,query,expected_doc",
    [
        ("frank", "what discount can a sales manager approve", "sales-discount-matrix"),
        ("amir", "reset break-glass administrator password", "rb-admin-password-reset"),
        ("bob", "salary band for dispatcher", "hr-comp-bands"),
        ("frank", "net payment term for Acme Housewares", "ctr-acme"),
        ("erin", "net payment term for Acme Housewares", "ctr-acme"),  # via user-level grant
        ("alice", "how do I troubleshoot VPN error 809", "rb-vpn"),
    ],
)
def test_authorised_users_get_the_right_document(pipeline, user, query, expected_doc):
    ans = pipeline.ask(user, query)
    assert not ans.refused
    assert ans.hits[0].chunk.doc_id == expected_doc
    assert any(c.doc_id == expected_doc for c in ans.citations)


@pytest.mark.parametrize(
    "user,query,forbidden_doc",
    [
        ("judy", "reset break-glass administrator password", "rb-admin-password-reset"),
        ("alice", "reset break-glass administrator password", "rb-admin-password-reset"),
        ("carol", "net payment term for Acme Housewares", "ctr-acme"),
        ("erin", "net payment term for Brightwave Electronics", "ctr-brightwave"),
        ("dan", "Northwind exclusivity liquidated damages", "ctr-northwind-strategic"),
        ("ivan", "salary band for dispatcher", "hr-comp-bands"),
        ("bob", "Project Harbour acquisition offer range", "exec-ma-target"),
        ("beth", "Project Harbour acquisition offer range", "exec-ma-target"),
    ],
)
def test_unauthorised_users_do_not_get_the_document(pipeline, user, query, forbidden_doc):
    ans = pipeline.ask(user, query)
    assert all(h.chunk.doc_id != forbidden_doc for h in ans.hits)
    assert all(c.doc_id != forbidden_doc for c in ans.citations)


def test_user_cannot_see_near_duplicate_restricted_doc(directory):
    """Public travel policy vs. exec-only addendum: a normal user gets the public one only."""
    p = pipeline_for("memory", "section")
    ans = p.ask("judy", "what hotel rate is allowed per night")
    cited = {c.doc_id for c in ans.citations}
    assert "exec-travel-addendum" not in cited
    assert "450" not in ans.answer
    ans_exec = p.ask("kai", "executive hotel rate per night business class")
    assert "exec-travel-addendum" in {c.doc_id for c in ans_exec.citations}


def test_unknown_user_is_rejected(pipeline):
    with pytest.raises(KeyError):
        pipeline.ask("mallory", "anything")


class _LeakyStore:
    """Simulates a buggy/compromised store that ignores the ACL filter entirely."""

    def __init__(self, inner):
        self.inner = inner

    def dense_search(self, query_vec, principals, k):
        everyone = frozenset().union(*(c.allow for c in self.inner._chunks))
        return self.inner.dense_search(query_vec, everyone, k)

    def visible_chunks(self, principals):
        return list(self.inner._chunks)

    def add(self, chunks, vectors):
        self.inner.add(chunks, vectors)

    def count(self):
        return self.inner.count()


def test_retriever_guard_blocks_a_store_that_ignores_acls(directory):
    base = pipeline_for("memory", "section")
    leaky = HybridRetriever(directory, _LeakyStore(base.retriever.store), base.retriever.embedder)
    res = leaky.retrieve("judy", "reset break-glass administrator password Project Harbour", k=4)
    assert res.acl_violations_blocked > 0
    for h in res.hits:
        assert can_access(h.chunk.allow, directory.effective_principals("judy"))


def test_refusal_is_uniform_and_does_not_reveal_restricted_documents(pipeline):
    """A refusal for 'exists but restricted' must look identical to 'does not exist'."""
    restricted = pipeline.ask("judy", "Project Harbour acquisition offer range")
    nonexistent = pipeline.ask("judy", "Project Zanzibar acquisition offer range")
    for ans in (restricted, nonexistent):
        assert "Harbour" not in ans.answer and "Zanzibar" not in ans.answer
        assert not ans.citations or all("exec" not in c.doc_id for c in ans.citations)
    if restricted.refused and nonexistent.refused:
        assert restricted.answer == nonexistent.answer == REFUSAL


@pytest.mark.parametrize(
    "query", ["what is the weather in Paris today", "recipe for lasagna", "asdf qwerty zxcv"]
)
def test_out_of_scope_queries_are_refused(pipeline, query):
    ans = pipeline.ask("ivan", query)
    assert ans.refused and not ans.citations and ans.answer == REFUSAL
