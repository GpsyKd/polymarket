"""Tests for the stage-0 screener (pure function, no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polybot.config import Settings
from polybot.data.models import Market
from polybot.screener.stage0 import screen_markets

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


def _market(mid: str, *, game_start: datetime | None = None) -> Market:
    data = {
        "id": mid,
        "question": f"Q{mid}",
        "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.5", "0.5"],
        "endDate": (NOW + timedelta(days=3)).isoformat(),
        "liquidity": "5000",
        "spread": 0.02,
    }
    if game_start is not None:
        data["gameStartTime"] = game_start.isoformat()
    return Market.model_validate(data)


def test_skips_started_events():
    s = Settings()
    started = _market("1", game_start=NOW - timedelta(minutes=5))   # already underway
    upcoming = _market("2", game_start=NOW + timedelta(hours=2))    # not yet started
    untimed = _market("3")                                          # no gameStartTime
    kept = {r.market.id for r in screen_markets([started, upcoming, untimed], s, now=NOW)}
    assert kept == {"2", "3"}  # started event dropped; others survive


def test_started_filter_can_be_disabled():
    s = Settings(screen_skip_started_events=False)
    started = _market("1", game_start=NOW - timedelta(minutes=5))
    kept = {r.market.id for r in screen_markets([started], s, now=NOW)}
    assert kept == {"1"}


def test_empty_game_start_time_parses_as_none():
    # Gamma sends "" for missing kickoff — must not crash and must not be dropped.
    m = Market.model_validate({
        "id": "x", "question": "Q", "outcomes": ["Yes", "No"],
        "outcomePrices": ["0.5", "0.5"], "endDate": (NOW + timedelta(days=3)).isoformat(),
        "liquidity": "5000", "spread": 0.02, "gameStartTime": "",
    })
    assert m.game_start_time is None
    assert {r.market.id for r in screen_markets([m], Settings(), now=NOW)} == {"x"}
