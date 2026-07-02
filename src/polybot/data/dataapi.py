"""Client for the Polymarket Data API (recent trades / on-chain activity)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)


@dataclass
class Trade:
    wallet: str
    side: str  # "BUY" | "SELL"
    outcome_index: int  # 0 = first outcome (Yes), 1 = No
    size: float  # shares
    price: float
    timestamp: int


class PolymarketDataClient:
    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=timeout, headers={"User-Agent": "polybot/0.1"}
        )

    async def __aenter__(self) -> "PolymarketDataClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def recent_trades(self, condition_id: str, limit: int = 100) -> list[Trade]:
        r = await self._client.get("/trades", params={"market": condition_id, "limit": limit})
        r.raise_for_status()
        raw = r.json()
        if not isinstance(raw, list):
            return []
        trades: list[Trade] = []
        for t in raw:
            parsed = _parse_trade(t)
            if parsed is not None:
                trades.append(parsed)
        return trades


def _parse_trade(t: dict[str, Any]) -> Trade | None:
    try:
        return Trade(
            wallet=str(t.get("proxyWallet", "")),
            side=str(t.get("side", "")).upper(),
            outcome_index=int(t.get("outcomeIndex", 0)),
            size=float(t.get("size", 0.0)),
            price=float(t.get("price", 0.0)),
            timestamp=int(t.get("timestamp", 0)),
        )
    except (TypeError, ValueError):
        return None
