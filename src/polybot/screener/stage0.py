"""Stage-0 screener: cheap, LLM-free filtering of the full market universe.

Narrows thousands of markets down to a handful worth spending signal/LLM
budget on. Filters on tradability (binary, active, liquid, resolvable soon,
priced in a sane band, tight spread) and ranks by a simple activity proxy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..config import Settings
from ..data.models import Market


@dataclass
class ScreenResult:
    market: Market
    hours_to_resolve: float | None
    score: float


def _hours_to_resolve(market: Market, now: datetime) -> float | None:
    if market.end_date is None:
        return None
    return (market.end_date - now).total_seconds() / 3600.0


def _event_started(market: Market, now: datetime) -> bool:
    """True once the underlying event's kickoff has passed. Only fires when the
    market carries a gameStartTime (sports/scheduled events); untimed markets
    (most non-sports) are never dropped by this filter."""
    gst = market.game_start_time
    if gst is None:
        return False
    if gst.tzinfo is None:
        gst = gst.replace(tzinfo=timezone.utc)
    return gst <= now


def screen_markets(
    markets: list[Market],
    settings: Settings,
    now: datetime | None = None,
) -> list[ScreenResult]:
    now = now or datetime.now(timezone.utc)
    results: list[ScreenResult] = []

    for m in markets:
        if m.closed or not m.active:
            continue
        if not m.is_binary:
            continue

        liquidity = m.liquidity or 0.0
        if liquidity < settings.screen_min_liquidity_usd:
            continue

        vol24 = m.volume_24hr or 0.0
        if vol24 < settings.screen_min_volume24h_usd:
            continue

        hours = _hours_to_resolve(m, now)
        if hours is None or hours < settings.screen_min_hours_to_resolve:
            continue
        if hours > settings.screen_max_days_to_resolve * 24:
            continue

        if settings.screen_skip_started_events and _event_started(m, now):
            continue

        price = m.yes_price()
        if price is not None and not (
            settings.screen_price_low <= price <= settings.screen_price_high
        ):
            continue

        if m.spread is not None and m.spread > settings.screen_max_spread:
            continue

        # Simple activity proxy; refined once real signals land.
        score = liquidity + vol24
        results.append(ScreenResult(market=m, hours_to_resolve=hours, score=score))

    results.sort(key=lambda r: r.score, reverse=True)
    return results
