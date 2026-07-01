"""Deterministic tests for the paper-trading math (no network)."""

from __future__ import annotations

from datetime import datetime, timezone

from polybot.data.models import Market
from polybot.paper.engine import settle, winner_label
from polybot.paper.metrics import build_report
from polybot.paper.sizing import decide_bet, kelly_fraction
from polybot.paper.storage import Position, Storage


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _market(**kw) -> Market:
    base = {"id": "1", "question": "Q", "outcomes": ["Yes", "No"]}
    base.update(kw)
    return Market.model_validate(base)


def test_kelly_fraction():
    assert abs(kelly_fraction(0.6, 0.5) - 0.2) < 1e-9
    assert kelly_fraction(0.5, 0.5) == 0.0
    assert kelly_fraction(0.6, 0.0) == 0.0  # invalid price


def test_decide_bet_yes_no_and_min_edge():
    kw = dict(bankroll=100, min_edge=0.05, kelly_mult=0.25,
              max_position=15, min_stake=1, remaining_exposure=100)

    yes = decide_bet(0.6, 0.5, **kw)
    assert yes is not None and yes.side == "YES"
    assert abs(yes.size_usd - 0.25 * 0.2 * 100) < 1e-6  # 5.0
    assert abs(yes.shares - yes.size_usd / 0.5) < 1e-9

    no = decide_bet(0.3, 0.5, **kw)
    assert no is not None and no.side == "NO"
    assert abs(no.price - 0.5) < 1e-9 and abs(no.prob - 0.7) < 1e-9

    assert decide_bet(0.52, 0.5, **kw) is None  # edge below floor

    capped = decide_bet(0.9, 0.5, **kw)
    assert capped is not None and capped.size_usd <= 15.0  # max_position cap


def test_winner_label_and_settle():
    assert winner_label(_market(closed=True, outcomePrices=["1", "0"])) == "Yes"
    assert winner_label(_market(closed=True, outcomePrices=["0", "1"])) == "No"
    assert winner_label(_market(closed=True, outcomePrices=["0.5", "0.5"])) is None
    assert winner_label(_market(closed=False, outcomePrices=["1", "0"])) is None

    ep, pnl, won = settle("YES", shares=10.0, size_usd=5.0, winner="Yes", outcomes=["Yes", "No"])
    assert won and ep == 1.0 and abs(pnl - 5.0) < 1e-9  # 10*1 - 5

    ep2, pnl2, won2 = settle("NO", shares=10.0, size_usd=5.0, winner="Yes", outcomes=["Yes", "No"])
    assert (not won2) and ep2 == 0.0 and abs(pnl2 + 5.0) < 1e-9


def test_storage_and_report(tmp_path):
    store = Storage(str(tmp_path / "t.sqlite3"))
    pos = Position(market_id="1", question="Q", side="YES", entry_price=0.5,
                   model_prob=0.6, edge=0.1, size_usd=5.0, shares=10.0, ts_open=_now())
    pid = store.insert_position(pos)
    assert pid == 1
    assert store.open_exposure() == 5.0
    assert store.open_market_ids() == {"1"}

    store.close_position(pid, exit_price=1.0, pnl=5.0, outcome="Yes", ts_close=_now())
    report = build_report(store.all_positions())

    assert report["n_closed"] == 1
    assert report["pnl_usd"] == 5.0
    assert report["hit_rate"] == 1.0
    # model prob 0.6, won=1 -> (0.6-1)^2 = 0.16 ; market baseline 0.5 -> 0.25
    assert abs(report["brier"] - 0.16) < 1e-9
    assert abs(report["brier_baseline_market"] - 0.25) < 1e-9
    assert report["beats_market"] is True
    store.close()
