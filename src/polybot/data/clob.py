"""Client for the Polymarket CLOB API (live order book)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@dataclass
class OrderBook:
    token_id: str
    bids: list[tuple[float, float]]  # (price, size)
    asks: list[tuple[float, float]]
    tick_size: float | None = None
    min_order_size: float | None = None
    last_trade_price: float | None = None

    @classmethod
    def from_api(cls, token_id: str, data: dict[str, Any]) -> "OrderBook":
        def levels(key: str) -> list[tuple[float, float]]:
            out: list[tuple[float, float]] = []
            for lvl in data.get(key) or []:
                p, s = _f(lvl.get("price")), _f(lvl.get("size"))
                if p is not None and s is not None:
                    out.append((p, s))
            return out

        return cls(
            token_id=token_id,
            bids=levels("bids"),
            asks=levels("asks"),
            tick_size=_f(data.get("tick_size")),
            min_order_size=_f(data.get("min_order_size")),
            last_trade_price=_f(data.get("last_trade_price")),
        )

    @property
    def best_bid(self) -> float | None:
        return max((p for p, _ in self.bids), default=None)

    @property
    def best_ask(self) -> float | None:
        return min((p for p, _ in self.asks), default=None)

    @property
    def mid(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return (bb + ba) / 2.0

    @property
    def spread(self) -> float | None:
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return None
        return ba - bb

    def imbalance(self, levels: int = 5) -> float | None:
        """Signed order-book imbalance over the top `levels` on each side.

        Returns a value in [-1, 1]: positive means bid-heavy (upward pressure).
        """
        top_bids = sorted(self.bids, key=lambda x: -x[0])[:levels]
        top_asks = sorted(self.asks, key=lambda x: x[0])[:levels]
        bid_depth = sum(s for _, s in top_bids)
        ask_depth = sum(s for _, s in top_asks)
        total = bid_depth + ask_depth
        if total <= 0:
            return None
        return (bid_depth - ask_depth) / total

    def microprice(self) -> float | None:
        """Size-weighted fair value at the top of book (Stoikov microprice)."""
        bb, ba = self.best_bid, self.best_ask
        if bb is None or ba is None:
            return self.mid
        bid_sz = sum(s for p, s in self.bids if p == bb)
        ask_sz = sum(s for p, s in self.asks if p == ba)
        if bid_sz + ask_sz <= 0:
            return self.mid
        return (bb * ask_sz + ba * bid_sz) / (bid_sz + ask_sz)


class ClobClient:
    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": "polybot/0.1"},
        )

    async def __aenter__(self) -> "ClobClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def fetch_book(self, token_id: str) -> OrderBook | None:
        r = await self._client.get("/book", params={"token_id": token_id})
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            return None
        return OrderBook.from_api(token_id, data)

    async def fetch_midpoint(self, token_id: str) -> float | None:
        r = await self._client.get("/midpoint", params={"token_id": token_id})
        r.raise_for_status()
        return _f(r.json().get("mid"))
