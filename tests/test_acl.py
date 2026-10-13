import pytest

from rag.acl import EVERYONE, Directory, Group, UnknownUserError, User, can_access
from rag.corpus import chunk_documents


def test_everyone_and_self_always_present(directory):
    p = directory.effective_principals("judy")
    assert p == frozenset({"user:judy", EVERYONE})


def test_group_implication_is_transitive(directory):
    assert "group:it" in directory.effective_principals("amir")  # it-admins implies it
    assert "group:finance" in directory.effective_principals("dan")
    assert "group:hr" in directory.effective_principals("beth")
    # ...but not the other way round
    assert "group:it-admins" not in directory.effective_principals("alice")
    assert "group:finance-managers" not in directory.effective_principals("carol")


def test_multi_group_user(directory):
    p = directory.effective_principals("heidi")
    assert {"group:exec", "group:finance-managers", "group:finance"} <= p


def test_unknown_user_raises_instead_of_defaulting(directory):
    with pytest.raises(UnknownUserError):
        directory.effective_principals("mallory")


def test_cycle_in_group_graph_terminates():
    d = Directory(
        users={"u": User(id="u", name="U", title="t", department="d", groups=["a"])},
        groups={"a": Group(id="a", implies=["b"]), "b": Group(id="b", implies=["a"])},
    )
    assert d.effective_principals("u") == frozenset({"user:u", EVERYONE, "group:a", "group:b"})


def test_unknown_group_in_membership_grants_nothing():
    d = Directory(
        users={"u": User(id="u", name="U", title="t", department="d", groups=["ghost"])},
        groups={},
    )
    assert d.effective_principals("u") == frozenset({"user:u", EVERYONE})


def test_can_access_semantics():
    p = frozenset({"user:x", EVERYONE})
    assert can_access({"group:everyone"}, p)
    assert can_access({"user:x"}, p)
    assert not can_access({"group:hr"}, p)
    assert not can_access(set(), p)  # empty ACL means nobody, not everybody


def test_every_document_has_a_nonempty_acl_and_unique_ref(docs):
    assert len(docs) >= 45
    assert all(d.allow for d in docs)
    assert len({d.ref for d in docs}) == len(docs)


@pytest.mark.parametrize("strategy", ["fixed", "section"])
def test_chunks_inherit_document_acl(docs, strategy):
    by_id = {d.id: d for d in docs}
    chunks = chunk_documents(docs, strategy)
    assert {c.doc_id for c in chunks} == set(by_id)
    assert all(c.allow == by_id[c.doc_id].allow for c in chunks)
    assert len({c.id for c in chunks}) == len(chunks)


def test_named_user_grant_on_contract(directory, docs):
    acme = next(d for d in docs if d.id == "ctr-acme")
    brightwave = next(d for d in docs if d.id == "ctr-brightwave")
    erin = directory.effective_principals("erin")
    assert can_access(acme.allow, erin)  # explicit user:erin grant
    assert not can_access(brightwave.allow, erin)  # sales (non-manager) has no contract access
    assert can_access(brightwave.allow, directory.effective_principals("frank"))
