"""Calibration & performance metrics over the paper ledger.

ROI / PnL are computed over all closed positions (flow exits + resolutions).
Brier and calibration are computed only over positions held to *resolution*
(terminal 0/1 outcome). Everything is also broken down `by_strategy` so the
paper->live gate can answer the real question: which signal actually has edge.
"""

from __future__ import annotations

from statistics import mean
from typing import Any

from .storage import Position


def build_report(positions: list[Position]) -> dict[str, Any]:
    closed = [p for p in positions if p.status == "closed"]
    open_ = [p for p in positions if p.status == "open"]

    report: dict[str, Any] = {
        "n_total": len(positions),
        "n_open": len(open_),
        "n_closed": len(closed),
        "open_exposure_usd": round(sum(p.size_usd for p in open_), 2),
    }
    if not closed:
        return report

    report.update(_metrics_for(closed))

    strategies = sorted({(p.strategy or "?") for p in closed})
    if len(strategies) > 1:
        report["by_strategy"] = {
            s: _metrics_for([p for p in closed if (p.strategy or "?") == s]) for s in strategies
        }
    return report


def _metrics_for(closed: list[Position]) -> dict[str, Any]:
    staked = sum(p.size_usd for p in closed)
    pnl = sum((p.pnl_usd or 0.0) for p in closed)
    wins = sum(1 for p in closed if (p.pnl_usd or 0.0) > 0)

    out: dict[str, Any] = {
        "n_closed": len(closed),
        "staked_usd": round(staked, 2),
        "pnl_usd": round(pnl, 2),
        "roi": round(pnl / staked, 4) if staked else 0.0,
        "win_rate": round(wins / len(closed), 4),
        "by_reason": _by_reason(closed),
    }

    resolved = [p for p in closed if p.close_reason == "resolution" and p.exit_price is not None]
    if resolved:
        won = [1.0 if (p.exit_price or 0.0) >= 0.5 else 0.0 for p in resolved]
        brier = mean((p.model_prob - w) ** 2 for p, w in zip(resolved, won))
        brier_market = mean((p.entry_price - w) ** 2 for p, w in zip(resolved, won))
        out["resolution"] = {
            "n": len(resolved),
            "brier": round(brier, 4),
            "brier_baseline_market": round(brier_market, 4),
            "beats_market": brier < brier_market,
            "calibration": _calibration(resolved, won),
        }
    return out


def _by_reason(closed: list[Position]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for p in closed:
        reason = p.close_reason or "unknown"
        bucket = out.setdefault(reason, {"n": 0, "pnl": 0.0})
        bucket["n"] += 1
        bucket["pnl"] += p.pnl_usd or 0.0
    for bucket in out.values():
        bucket["pnl"] = round(bucket["pnl"], 2)
    return out


def _calibration(resolved: list[Position], won: list[float]) -> list[dict[str, Any]]:
    buckets: dict[int, list[tuple[float, float]]] = {}
    for p, w in zip(resolved, won):
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
