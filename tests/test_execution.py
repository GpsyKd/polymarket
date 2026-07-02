"""Tests for the execution layer (paper + live guards; no real SDK, no money)."""

from __future__ import annotations

import asyncio
import sys
import types

from polybot.config import Settings
from polybot.execution.live import LIVE_CONFIRM_SENTINEL, LiveExecutor
from polybot.execution.paper import PaperExecutor


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# paper executor
# --------------------------------------------------------------------------- #
def test_paper_executor():
    ex = PaperExecutor()
    assert ex.mode == "paper"
    f = _run(ex.buy("t", 0.5, 10.0, 0.01))
    assert f is not None and abs(f.shares - 20.0) < 1e-9 and f.size_usd == 10.0
    assert _run(ex.buy("t", 0.0, 10.0, 0.01)) is None  # invalid price
    s = _run(ex.sell("t", 0.6, 20.0, 0.01))
    assert s is not None and abs(s.size_usd - 12.0) < 1e-9
    assert _run(ex.usdc_balance()) is None


# --------------------------------------------------------------------------- #
# live executor guards (no SDK needed for these paths)
# --------------------------------------------------------------------------- #
def _settings(**kw) -> Settings:
    base = dict(mode="live", max_position_usd=15.0)
    base.update(kw)
    return Settings(**base)


def test_live_dry_does_not_order():
    # live_confirm unset → not armed → dry-live: buy/sell return None, never touch the client
    ex = LiveExecutor(_settings(live_confirm=None), private_key="0xkey", client=object())
    assert ex.armed is False
    assert _run(ex.buy("token", 0.5, 5.0, 0.01)) is None
    assert _run(ex.sell("token", 0.5, 10.0, 0.01)) is None


def test_live_size_cap_blocks():
    ex = LiveExecutor(_settings(live_confirm=LIVE_CONFIRM_SENTINEL, max_position_usd=15.0),
                      private_key="0xkey", client=object())
    assert ex.armed is True
    # over the hard cap → blocked before any order construction
    assert _run(ex.buy("token", 0.5, 999.0, 0.01)) is None


class _FakeClob:
    def __init__(self):
        self.orders = []

    def create_and_post_market_order(self, args, opts, order_type):
        self.orders.append(("market", args, order_type))
        return {"orderID": "mkt-1"}

    def create_and_post_order(self, args, opts, order_type):
        self.orders.append(("limit", args, order_type))
        return {"orderID": "lim-1"}


def _install_fake_sdk() -> None:
    m = types.ModuleType("py_clob_client_v2")

    class MarketOrderArgs:
        def __init__(self, token_id, amount, side):
            self.token_id, self.amount, self.side = token_id, amount, side

    class OrderArgs:
        def __init__(self, token_id, price, size, side):
            self.token_id, self.price, self.size, self.side = token_id, price, size, side

    class PartialCreateOrderOptions:
        def __init__(self, tick_size):
            self.tick_size = tick_size

    class OrderType:
        FAK, GTC = "FAK", "GTC"

    class Side:
        BUY, SELL = "BUY", "SELL"

    class ClobClient:  # not used (client injected) but present for completeness
        def __init__(self, **kw):
            pass

    for name, obj in dict(
        MarketOrderArgs=MarketOrderArgs, OrderArgs=OrderArgs,
        PartialCreateOrderOptions=PartialCreateOrderOptions, OrderType=OrderType,
        Side=Side, ClobClient=ClobClient,
    ).items():
        setattr(m, name, obj)
    sys.modules["py_clob_client_v2"] = m


def test_live_armed_places_orders():
    _install_fake_sdk()
    try:
        fake = _FakeClob()
        ex = LiveExecutor(_settings(live_confirm=LIVE_CONFIRM_SENTINEL, max_position_usd=15.0),
                          private_key="0xkey", client=fake)
        fill = _run(ex.buy("token123", 0.5, 5.0, 0.01))
        assert fill is not None and fill.order_id == "mkt-1"
        kind, args, _ = fake.orders[0]
        assert kind == "market" and args.token_id == "token123" and args.side == "BUY"
        assert abs(args.amount - 5.0) < 1e-9

        sell = _run(ex.sell("token123", 0.6, 8.0, 0.01))
        assert sell is not None and sell.order_id == "lim-1"
        kind2, args2, _ = fake.orders[1]
        assert kind2 == "limit" and args2.side == "SELL" and abs(args2.price - 0.6) < 1e-9
    finally:
        sys.modules.pop("py_clob_client_v2", None)
