"""Golden-set schema and loader (see evals/README.md)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

GOLDEN_PATH = Path(__file__).resolve().parent / "golden.jsonl"

Category = Literal[
    "single_doc",
    "multi_hop",
    "acl_boundary",
    "unanswerable",
    "ambiguous",
    "prompt_injection",
    "pii",
    "write_action",
]


class Turn(BaseModel):
    role: Literal["user", "assistant"]
    content: str


class Scenario(BaseModel):
    id: str
    category: Category
    user_id: str
    input: str | list[Turn]
    expected: str
    key_facts: list[str] = Field(default_factory=list)  # extension: deterministic fact recall
    required_sources: list[str] = Field(default_factory=list)
    forbidden_sources: list[str] = Field(default_factory=list)
    should_refuse: bool = False
    canary: str | None = None
    needs_human_review: bool = False

    @property
    def message(self) -> str:
        """The final user message (what `answer()` receives)."""
        return self.input if isinstance(self.input, str) else self.input[-1].content

    @property
    def history(self) -> list[Turn] | None:
        return None if isinstance(self.input, str) else self.input[:-1]


def load_golden(path: Path = GOLDEN_PATH) -> list[Scenario]:
    return [
        Scenario.model_validate(json.loads(line))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
