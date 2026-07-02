"""Whale-flow signal: lean toward the side large ("whale") trades are taking.

For a market we pull recent trades and net the notional of large trades by their
directional effect on P(YES). Strong one-sided whale flow nudges our estimate —
the thesis being that big money is more likely to be informed. Held to
resolution and graded by Brier; whether it actually has edge is what the paper
ledger measures.
"""

from __future__ import annotations

import logging

from ..data.dataapi import PolymarketDataClient, Trade
from ..data.models import Market
from ..paper.strategy import Signal

log = logging.getLogger(__name__)


def whale_signal(
    trades: list[Trade],
    yes_price: float,
    min_whale_usd: float,
    edge_scale: float,
    min_flow: float,
) -> Signal | None:
    """Net large-trade flow → a small directional lean on P(YES), or None."""
    net = 0.0
    total = 0.0
    for t in trades:
        usd = t.size * t.price
        if usd < min_whale_usd:
            continue
        # BUY of Yes(0) or SELL of No(1) is bullish on YES.
        bullish_yes = (t.side == "BUY") == (t.outcome_index == 0)
        net += usd if bullish_yes else -usd
        total += usd

    if total <= 0.0:
        return None
    flow = net / total  # in [-1, 1]
    if abs(flow) < min_flow:
        return None

    prob = min(max(yes_price + edge_scale * flow, 0.02), 0.98)
    confidence = min(1.0, total / (min_whale_usd * 4.0))
    return Signal(prob_yes=prob, confidence=confidence, rationale=f"whale flow {flow:+.2f} / ${total:,.0f}")


class WhaleAnalyzer:
    name = "whale-flow"
    horizon = "resolution"

    def __init__(
        self,
        data_client: PolymarketDataClient,
        min_whale_usd: float = 500.0,
        edge_scale: float = 0.08,
        min_flow: float = 0.3,
        trades_limit: int = 100,
    ) -> None:
        self.data_client = data_client
        self.min_whale_usd = min_whale_usd
        self.edge_scale = edge_scale
        self.min_flow = min_flow
        self.trades_limit = trades_limit

    async def analyze(self, market: Market, yes_price: float) -> Signal | None:
        if not market.condition_id:
            return None
        try:
            trades = await self.data_client.recent_trades(market.condition_id, self.trades_limit)
        except Exception as e:  # noqa: BLE001
            log.debug("trades fetch failed for %s: %s", market.condition_id, e)
            return None
        return whale_signal(trades, yes_price, self.min_whale_usd, self.edge_scale, self.min_flow)
