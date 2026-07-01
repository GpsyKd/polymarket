"""Calibration & performance metrics over the paper ledger.

The paper->live gate is defined here in spirit: enough closed bets, positive
ROI after spread, and a Brier score that beats the market-implied baseline.
"""

from __future__ import annotations

from statistics import mean
from typing import Any

from .storage import Position


def _won(p: Position) -> float:
    return 1.0 if (p.exit_price or 0.0) >= 0.5 else 0.0


def build_report(positions: list[Position]) -> dict[str, Any]:
    closed = [p for p in positions if p.status == "closed" and p.exit_price is not None]
    open_ = [p for p in positions if p.status == "open"]

    report: dict[str, Any] = {
        "n_total": len(positions),
        "n_open": len(open_),
        "n_closed": len(closed),
        "open_exposure_usd": round(sum(p.size_usd for p in open_), 2),
    }

    if not closed:
        return report

    staked = sum(p.size_usd for p in closed)
    pnl = sum((p.pnl_usd or 0.0) for p in closed)
    won = [_won(p) for p in closed]

    brier = mean((p.model_prob - w) ** 2 for p, w in zip(closed, won))
    brier_market = mean((p.entry_price - w) ** 2 for p, w in zip(closed, won))

    report.update({
        "staked_usd": round(staked, 2),
        "pnl_usd": round(pnl, 2),
        "roi": round(pnl / staked, 4) if staked else 0.0,
        "hit_rate": round(sum(won) / len(closed), 4),
        "brier": round(brier, 4),
        "brier_baseline_market": round(brier_market, 4),
        "beats_market": brier < brier_market,
        "calibration": _calibration(closed, won),
    })
    return report


def _calibration(closed: list[Position], won: list[float]) -> list[dict[str, Any]]:
    buckets: dict[int, list[tuple[float, float]]] = {}
    for p, w in zip(closed, won):
        b = min(int(p.model_prob * 10), 9)
        buckets.setdefault(b, []).append((p.model_prob, w))
    out = []
    for b in sorted(buckets):
        rows = buckets[b]
        out.append({
            "bucket": f"{b / 10:.1f}-{(b + 1) / 10:.1f}",
            "n": len(rows),
            "pred": round(mean(x for x, _ in rows), 3),
            "actual": round(mean(y for _, y in rows), 3),
        })
    return out
