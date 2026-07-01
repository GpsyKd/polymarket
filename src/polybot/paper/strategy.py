"""Strategy interface + a Phase-1 placeholder.

A Strategy turns a market (and optional live order book) into a probability
estimate. Real signals (news+LLM, microstructure, social, whales) will each
implement this interface in Phase 2.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..data.clob import OrderBook
from ..data.models import Market


@dataclass
class Signal:
    prob_yes: float  # estimated P(Yes)
    confidence: float
    rationale: str


class Strategy(Protocol):
    name: str

    def evaluate(self, market: Market, yes_price: float, book: OrderBook | None) -> Signal | None:
        ...


class PlaceholderStrategy:
    """NOT a real edge — a stand-in so the paper loop and metrics can be
    exercised end-to-end before real signals exist.

    Heuristic: nudge the estimate `pull` points toward 0.5 (naive mean
    reversion), clamped so it never crosses the midpoint. Its PnL is
    meaningless as a measure of strategy quality; it only proves the pipeline
    works. Replace in Phase 2.
    """

    name = "placeholder-meanrev"

    def __init__(self, pull: float = 0.06) -> None:
        self.pull = pull

    def evaluate(self, market: Market, yes_price: float, book: OrderBook | None) -> Signal | None:
        if not (0.0 < yes_price < 1.0):
            return None
        if yes_price < 0.5:
            prob = min(yes_price + self.pull, 0.5)
        else:
            prob = max(yes_price - self.pull, 0.5)
        prob = min(max(prob, 0.02), 0.98)
        return Signal(prob_yes=prob, confidence=0.2, rationale=f"placeholder meanrev pull={self.pull}")
