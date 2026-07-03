"""Tests for the whale-flow signal (pure function, no network)."""

from __future__ import annotations

from polybot.data.dataapi import Trade
from polybot.whale.analyzer import whale_signal


def _t(side: str, idx: int, size: float, price: float, ts: int = 0) -> Trade:
    return Trade(wallet="w", side=side, outcome_index=idx, size=size, price=price, timestamp=ts)


def test_direction():
    # big BUY of Yes(0) → bullish → prob above price
    assert whale_signal([_t("BUY", 0, 10000, 0.5)], 0.5, 500, 0.08, 0.3).prob_yes > 0.5
    # big BUY of No(1) → bearish on YES
    assert whale_signal([_t("BUY", 1, 10000, 0.5)], 0.5, 500, 0.08, 0.3).prob_yes < 0.5
    # SELL of No(1) → bullish on YES
    assert whale_signal([_t("SELL", 1, 10000, 0.5)], 0.5, 500, 0.08, 0.3).prob_yes > 0.5
    # SELL of Yes(0) → bearish on YES
    assert whale_signal([_t("SELL", 0, 10000, 0.5)], 0.5, 500, 0.08, 0.3).prob_yes < 0.5


def test_thresholds():
    # trades below whale notional are ignored → nothing left → None
    assert whale_signal([_t("BUY", 0, 10, 0.5)], 0.5, 500, 0.08, 0.3) is None
    # balanced flow (equal $ both directions) → |flow| < min_flow → None
    balanced = [_t("BUY", 0, 2000, 0.5), _t("BUY", 1, 2000, 0.5)]
    assert whale_signal(balanced, 0.5, 500, 0.08, 0.3) is None
    assert whale_signal([], 0.5, 500, 0.08, 0.3) is None


def test_flow_magnitude():
    # one-sided flow=1.0 → prob = price + edge_scale = 0.58
    sig = whale_signal([_t("BUY", 0, 2000, 0.5)], 0.5, 500, 0.08, 0.3)
    assert sig is not None and abs(sig.prob_yes - 0.58) < 1e-9
    assert "whale flow" in sig.rationale


def test_stale_trades_ignored():
    # whale trade from before min_ts is already priced in → no signal
    old = [_t("BUY", 0, 2000, 0.5, ts=1_000)]
    assert whale_signal(old, 0.5, 500, 0.08, 0.3, min_ts=2_000) is None
    fresh = [_t("BUY", 0, 2000, 0.5, ts=3_000)]
    assert whale_signal(fresh, 0.5, 500, 0.08, 0.3, min_ts=2_000) is not None
    # mixed: only the fresh trade counts
    mixed = [_t("BUY", 0, 2000, 0.5, ts=1_000), _t("SELL", 0, 2000, 0.5, ts=3_000)]
    sig = whale_signal(mixed, 0.5, 500, 0.08, 0.3, min_ts=2_000)
    assert sig is not None and sig.prob_yes < 0.5
