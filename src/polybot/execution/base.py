"""Execution abstraction.

An Executor turns a decision to buy/sell a specific outcome token into a fill.
The paper executor simulates fills; the live executor places real orders on the
Polymarket CLOB. Everything above this layer (engine, sizing, risk caps) is
identical for paper and live — only the executor differs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class Fill:
    price: float        # average fill price per share (0..1)
    shares: float       # shares filled
    size_usd: float     # USD actually spent (buy) or received (sell)
    order_id: str | None = None


class Executor(Protocol):
    mode: str

    async def buy(self, token_id: str | None, price: float, size_usd: float, tick_size: float) -> Fill | None:
        ...

    async def sell(self, token_id: str | None, price: float, shares: float, tick_size: float) -> Fill | None:
        ...

    async def usdc_balance(self) -> float | None:
        ...

    async def aclose(self) -> None:
        ...
