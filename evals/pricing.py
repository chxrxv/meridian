"""Configurable price table. No built-in prices: unknown means unknown (cost = None)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PRICES = Path(__file__).resolve().parent / "prices.json"


@dataclass
class PriceTable:
    version: str
    per_million: dict[str, dict[str, float | None]]

    @classmethod
    def load(cls, path: Path | str = DEFAULT_PRICES) -> PriceTable:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(raw.get("version", "unset"), raw.get("per_million_tokens", {}))

    def cost(self, model: str | None, tokens_in: int, tokens_out: int) -> float | None:
        """USD cost of one call: 0.0 if no model was involved, None if no price is configured."""
        if model is None:
            return 0.0
        p = self.per_million.get(model)
        if not p or p.get("input") is None or p.get("output") is None:
            return None
        return (tokens_in * p["input"] + tokens_out * p["output"]) / 1_000_000
