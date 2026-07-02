"""Paper executor — simulates fills at the requested price (no network, no money)."""

from __future__ import annotations

from .base import Fill


class PaperExecutor:
    mode = "paper"

    async def buy(self, token_id: str | None, price: float, size_usd: float, tick_size: float) -> Fill | None:
        if not (0.0 < price < 1.0) or size_usd <= 0.0:
            return None
        return Fill(price=price, shares=size_usd / price, size_usd=size_usd)

    async def sell(self, token_id: str | None, price: float, shares: float, tick_size: float) -> Fill | None:
        price = min(max(price, 0.0), 1.0)
        return Fill(price=price, shares=shares, size_usd=shares * price)

    async def usdc_balance(self) -> float | None:
        return None

    async def aclose(self) -> None:
        return None
