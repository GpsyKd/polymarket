"""Strategy interface + implementations.

A Strategy turns a market (and optional live order book) into a probability
estimate. Real signals (microstructure, whales, news+LLM, social) each
implement this interface.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ..data.clob import OrderBook
from ..data.models import Market


@dataclass
class Signal:
    prob_yes: float  # estimated P(Yes) — for flow strategies, a short-horizon lean
    confidence: float
    rationale: str


class Strategy(Protocol):
    name: str
    horizon: str  # "resolution" (hold to settle) | "flow" (short-horizon, mark-to-market)

    def evaluate(self, market: Market, yes_price: float, book: OrderBook | None) -> Signal | None:
        ...


class PlaceholderStrategy:
    """NOT a real edge — a stand-in so the paper loop and metrics can be
    exercised end-to-end. Naive mean reversion toward 0.5. Its PnL is
    meaningless as a measure of strategy quality; it only proves the pipeline.
    """

    name = "placeholder-meanrev"
    horizon = "resolution"

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


class MicrostructureStrategy:
    """First real signal: order-book imbalance → short-horizon directional lean.

    Strong bid-heavy imbalance implies near-term upward pressure on the YES
    price (and vice-versa). This is a *flow* signal — it predicts short-term
    drift, not the terminal outcome, so positions are exited via mark-to-market
    (take-profit / stop-loss / max-hold), not held to resolution.

    Whether this actually has edge on Polymarket is an open question — the paper
    ledger is exactly how we find out.
    """

    name = "microstructure-imbalance"
    horizon = "flow"

    def __init__(self, min_imbalance: float = 0.35, levels: int = 5, edge_scale: float = 0.06) -> None:
        self.min_imbalance = min_imbalance
        self.levels = levels
        self.edge_scale = edge_scale

    def evaluate(self, market: Market, yes_price: float, book: OrderBook | None) -> Signal | None:
        if book is None or not (0.0 < yes_price < 1.0):
            return None
        imb = book.imbalance(self.levels)
        if imb is None or abs(imb) < self.min_imbalance:
            return None
        # Lean off the current price in the direction of imbalance.
        prob = min(max(yes_price + self.edge_scale * imb, 0.02), 0.98)
        return Signal(
            prob_yes=prob,
            confidence=abs(imb),
            rationale=f"imbalance={imb:+.2f} (top{self.levels})",
        )
