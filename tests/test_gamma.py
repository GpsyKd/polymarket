"""Tests for Gamma pagination resilience (no network; page fetch is stubbed)."""

from __future__ import annotations

import asyncio

import httpx

from polybot.data.gamma import GammaClient


def _run(coro):
    return asyncio.run(coro)


def _raise_422() -> None:
    req = httpx.Request("GET", "http://x/markets")
    raise httpx.HTTPStatusError("422", request=req, response=httpx.Response(422, request=req))


def _mk(i: int) -> dict:
    return {"id": str(i), "question": "Q", "outcomes": ["Yes", "No"],
            "outcomePrices": ["0.5", "0.5"]}


def test_offset_cap_returns_partial_not_crash():
    # Gamma 422s once the offset passes its hard cap — we keep the partial set.
    g = GammaClient("http://x")

    async def fake(*, limit, offset, active, closed, order, ascending):
        if offset >= 300:
            _raise_422()
        return [_mk(offset + j) for j in range(limit)]

    g._get_markets_page = fake  # type: ignore[assignment]
    out = _run(g.fetch_markets(page_size=100, max_markets=2000))
    assert len(out) == 300  # 3 full pages, then 422 → stop gracefully
    _run(g.aclose())


def test_order_param_rejected_retries_unordered_from_top():
    # An ordered request rejected at offset 0 → drop the order and restart clean.
    g = GammaClient("http://x")
    calls: list[tuple[int, str | None]] = []

    async def fake(*, limit, offset, active, closed, order, ascending):
        calls.append((offset, order))
        if order is not None:
            _raise_422()
        if offset >= 200:
            return []
        return [_mk(offset + j) for j in range(limit)]

    g._get_markets_page = fake  # type: ignore[assignment]
    out = _run(g.fetch_markets(page_size=100, max_markets=2000))
    assert calls[0] == (0, "volume24hr") and calls[1] == (0, None)
    assert len(out) == 200
    _run(g.aclose())
