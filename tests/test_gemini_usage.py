"""GeminiAnswerer token accounting, using a fake client (no network, no credentials)."""

from types import SimpleNamespace

from rag.generate import REFUSAL, GeminiAnswerer
from rag.types import Chunk, Hit


def _hits(n: int = 2) -> list[Hit]:
    return [
        Hit(Chunk(f"d{i}#0", f"d{i}", f"Doc {i}", "policy", "H", f"Text {i}.", frozenset(), 0))
        for i in range(n)
    ]


class FakeModels:
    def __init__(self, text, usage=None, raises=False):
        self.text, self.usage, self.raises, self.calls = text, usage, raises, []

    def generate_content(self, model, contents):
        self.calls.append((model, contents))
        if self.raises:
            raise RuntimeError("boom")
        return SimpleNamespace(text=self.text, usage_metadata=self.usage)


def _answerer(models):
    return GeminiAnswerer(model="fake-model", client=SimpleNamespace(models=models))


def test_last_usage_records_tokens_from_usage_metadata():
    usage = SimpleNamespace(prompt_token_count=321, candidates_token_count=45)
    a = _answerer(FakeModels("The limit is 12 percent [1].", usage))
    out = a.answer("limit?", _hits(), {})
    assert out == "The limit is 12 percent [1]."
    assert a.last_usage == {"input": 321, "output": 45}
    assert a.model == "fake-model"


def test_last_usage_is_reset_between_calls_and_none_on_api_failure():
    a = _answerer(FakeModels("ok [1]", SimpleNamespace(prompt_token_count=1, candidates_token_count=2)))
    a.answer("q", _hits(), {})
    assert a.last_usage == {"input": 1, "output": 2}
    a._client = SimpleNamespace(models=FakeModels("", raises=True))
    a.answer("q", _hits(), {})  # falls back to extractive
    assert a.last_usage is None


def test_missing_usage_metadata_gives_none_and_none_counts_become_zero():
    a = _answerer(FakeModels("ok [1]", None))
    a.answer("q", _hits(), {})
    assert a.last_usage is None
    a2 = _answerer(FakeModels("ok [1]", SimpleNamespace(prompt_token_count=None, candidates_token_count=None)))
    a2.answer("q", _hits(), {})
    assert a2.last_usage == {"input": 0, "output": 0}


def test_not_found_and_invalid_citations_still_behave_as_before():
    usage = SimpleNamespace(prompt_token_count=5, candidates_token_count=1)
    assert _answerer(FakeModels("NOT_FOUND", usage)).answer("q", _hits(), {}) == REFUSAL
    # citation index out of range => falls back to extractive text (not the model text)
    out = _answerer(FakeModels("Answer [9].", usage)).answer("text", _hits(), {"text": 1.0})
    assert "[9]" not in out
