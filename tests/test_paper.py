"""Deterministic tests for the paper-trading math (no network)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from polybot.data.clob import OrderBook
from polybot.data.models import Market
from polybot.paper.engine import exit_decision, settle, winner_label
from polybot.paper.metrics import build_report
from polybot.paper.sizing import decide_bet, kelly_fraction
from polybot.paper.storage import Position, Storage
from polybot.paper.strategy import MicrostructureStrategy


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _market(**kw) -> Market:
    base = {"id": "1", "question": "Q", "outcomes": ["Yes", "No"]}
    base.update(kw)
    return Market.model_validate(base)


# --------------------------------------------------------------------------- #
# sizing
# --------------------------------------------------------------------------- #
def test_kelly_fraction():
    assert abs(kelly_fraction(0.6, 0.5) - 0.2) < 1e-9
    assert kelly_fraction(0.5, 0.5) == 0.0
    assert kelly_fraction(0.6, 0.0) == 0.0


def test_decide_bet_yes_no_and_min_edge():
    kw = dict(bankroll=100, min_edge=0.05, kelly_mult=0.25,
              max_position=15, min_stake=1, remaining_exposure=100)
    yes = decide_bet(0.6, 0.5, **kw)
    assert yes is not None and yes.side == "YES"
    assert abs(yes.size_usd - 0.25 * 0.2 * 100) < 1e-6  # 5.0
    no = decide_bet(0.3, 0.5, **kw)
    assert no is not None and no.side == "NO" and abs(no.prob - 0.7) < 1e-9
    assert decide_bet(0.52, 0.5, **kw) is None  # edge below floor
    assert decide_bet(0.9, 0.5, **kw).size_usd <= 15.0  # max_position cap
    # spread fee raises the fill price and shrinks edge
    fee_bet = decide_bet(0.6, 0.5, fee=0.04, **kw)
    assert fee_bet is not None and abs(fee_bet.price - 0.54) < 1e-9 and fee_bet.edge > 0.05
    assert decide_bet(0.56, 0.5, fee=0.05, **kw) is None  # edge ~0.01 < floor after fee


# --------------------------------------------------------------------------- #
# order-book analytics + microstructure strategy
# --------------------------------------------------------------------------- #
def test_orderbook_imbalance_microprice():
    ob = OrderBook("t", bids=[(0.49, 800), (0.48, 200)], asks=[(0.51, 100), (0.52, 100)])
    assert ob.best_bid == 0.49 and ob.best_ask == 0.51 and abs(ob.mid - 0.5) < 1e-9
    # bids 1000 vs asks 200 -> (1000-200)/1200
    assert abs(ob.imbalance(5) - (800.0 / 1200.0)) < 1e-9
    # microprice leans toward the ask when bid-heavy
    assert ob.microprice() > 0.5


def test_micro_strategy_direction():
    s = MicrostructureStrategy(min_imbalance=0.2, levels=5, edge_scale=0.06)
    up = OrderBook("t", bids=[(0.49, 900)], asks=[(0.51, 100)])
    sig = s.evaluate(_market(), 0.5, up)
    assert sig is not None and sig.prob_yes > 0.5  # bid-heavy -> lean up
    flat = OrderBook("t", bids=[(0.49, 100)], asks=[(0.51, 100)])
    assert s.evaluate(_market(), 0.5, flat) is None  # imbalance 0 < min


# --------------------------------------------------------------------------- #
# settlement + flow exits
# --------------------------------------------------------------------------- #
def test_winner_label_and_settle():
    assert winner_label(_market(closed=True, outcomePrices=["1", "0"])) == "Yes"
    assert winner_label(_market(closed=True, outcomePrices=["0", "1"])) == "No"
    assert winner_label(_market(closed=True, outcomePrices=["0.5", "0.5"])) is None
    assert winner_label(_market(closed=False, outcomePrices=["1", "0"])) is None

    ep, pnl, won = settle("YES", 10.0, 5.0, "Yes", ["Yes", "No"])
    assert won and ep == 1.0 and abs(pnl - 5.0) < 1e-9
    ep2, pnl2, won2 = settle("NO", 10.0, 5.0, "Yes", ["Yes", "No"])
    assert (not won2) and ep2 == 0.0 and abs(pnl2 + 5.0) < 1e-9


def test_exit_decision():
    now = datetime(2026, 7, 2, tzinfo=timezone.utc)
    recent = (now - timedelta(hours=1)).isoformat()

    # held token entered at 0.50, now 0.55 -> +0.05 gain -> take-profit
    tp = exit_decision(0.50, 0.55, recent, now, tp=0.03, sl=0.05, max_hold_hours=48)
    assert tp is not None and tp[1] == "take_profit" and abs(tp[0] - 0.55) < 1e-9

    sl = exit_decision(0.50, 0.44, recent, now, tp=0.03, sl=0.05, max_hold_hours=48)
    assert sl[1] == "stop_loss"

    # half-spread fee on exit lowers the realised price
    fee_tp = exit_decision(0.50, 0.55, recent, now, 0.03, 0.05, 48, fee=0.01)
    assert fee_tp[1] == "take_profit" and abs(fee_tp[0] - 0.54) < 1e-9

    assert exit_decision(0.50, 0.51, recent, now, 0.03, 0.05, 48) is None

    old = (now - timedelta(hours=50)).isoformat()
    assert exit_decision(0.50, 0.505, old, now, 0.03, 0.05, 48)[1] == "max_hold"


# --------------------------------------------------------------------------- #
# storage + metrics
# --------------------------------------------------------------------------- #
def test_storage_and_report(tmp_path):
    store = Storage(str(tmp_path / "t.sqlite3"))

    won = Position(market_id="1", question="Q1", side="YES", entry_price=0.5,
                   model_prob=0.6, edge=0.1, size_usd=5.0, shares=10.0, ts_open=_now())
    pid = store.insert_position(won)
    assert pid == 1 and store.open_exposure() == 5.0 and store.open_market_ids() == {"1"}

    flow = Position(market_id="2", question="Q2", side="YES", entry_price=0.50,
                    model_prob=0.53, edge=0.03, size_usd=4.0, shares=8.0, ts_open=_now())
    store.insert_position(flow)

    store.close_position(1, exit_price=1.0, pnl=5.0, outcome="Yes",
                         ts_close=_now(), close_reason="resolution")
    store.close_position(2, exit_price=0.53, pnl=0.24, outcome="exit@0.530",
                         ts_close=_now(), close_reason="take_profit")

    report = build_report(store.all_positions())
    assert report["n_closed"] == 2
    assert abs(report["pnl_usd"] - 5.24) < 1e-9
    assert report["by_reason"]["resolution"]["n"] == 1
    assert report["by_reason"]["take_profit"]["n"] == 1

    res = report["resolution"]
    assert res["n"] == 1  # only the resolution-closed bet
    assert abs(res["brier"] - 0.16) < 1e-9          # (0.6-1)^2
    assert abs(res["brier_baseline_market"] - 0.25) < 1e-9  # (0.5-1)^2
    assert res["beats_market"] is True
    store.close()


def test_market_group_key():
    neg = _market(negRiskMarketID="0xabc")
    assert neg.group_key() == "neg:0xabc"
    evt = Market.model_validate(
        {"id": "2", "question": "Q", "outcomes": ["Yes", "No"], "events": [{"id": "30615", "slug": "wc"}]}
    )
    assert evt.group_key() == "evt:30615"
    assert _market(id="3").group_key() == "mkt:3"


def test_exposure_by_group(tmp_path):
    store = Storage(str(tmp_path / "g.sqlite3"))
    for mid, gk in [("1", "neg:A"), ("2", "neg:A"), ("3", "neg:B")]:
        store.insert_position(Position(
            market_id=mid, question="Q", side="YES", entry_price=0.5, model_prob=0.6,
            edge=0.1, size_usd=3.0, shares=6.0, ts_open=_now(), group_key=gk,
        ))
    exposure = store.exposure_by_group()
    assert exposure == {"neg:A": 6.0, "neg:B": 3.0}
    store.close()


def test_recently_analyzed(tmp_path):
    store = Storage(str(tmp_path / "a.sqlite3"))
    store.record_analysis("1", 0.6, 0.8, "grok-4.3", "2026-07-02T10:00:00+00:00")
    store.record_analysis("2", 0.4, 0.7, "grok-4.3", "2026-07-02T08:00:00+00:00")
    assert store.recently_analyzed_ids("2026-07-02T09:00:00+00:00") == {"1"}
    assert store.recently_analyzed_ids("2026-07-02T07:00:00+00:00") == {"1", "2"}
    assert store.recently_analyzed_ids("2026-07-02T11:00:00+00:00") == set()
    store.close()


def test_realized_pnl_since(tmp_path):
    store = Storage(str(tmp_path / "p.sqlite3"))
    store.insert_position(Position(
        market_id="1", question="Q", side="YES", entry_price=0.5, model_prob=0.6,
        edge=0.1, size_usd=5.0, shares=10.0, ts_open=_now(),
    ))
    store.close_position(1, 0.0, -5.0, "No", "2026-07-02T10:00:00+00:00", "resolution")
    assert store.realized_pnl_since("2026-07-02T09:00:00+00:00") == -5.0
    assert store.realized_pnl_since("2026-07-02T11:00:00+00:00") == 0.0
    store.close()


def test_storage_mode_isolation(tmp_path):
    store = Storage(str(tmp_path / "m.sqlite3"))
    store.insert_position(Position(market_id="1", question="Q", side="YES", entry_price=0.5,
                                   model_prob=0.6, edge=0.1, size_usd=5.0, shares=10.0,
                                   ts_open=_now(), mode="paper"))
    store.insert_position(Position(market_id="2", question="Q", side="YES", entry_price=0.5,
                                   model_prob=0.6, edge=0.1, size_usd=7.0, shares=14.0,
                                   ts_open=_now(), mode="live"))
    assert store.open_exposure(mode="paper") == 5.0
    assert store.open_exposure(mode="live") == 7.0
    assert store.open_exposure() == 12.0  # unfiltered view still available
    assert store.open_market_ids(mode="live") == {"2"}
    assert [p.market_id for p in store.open_positions(mode="paper")] == ["1"]
    store.close()


def test_storage_flags(tmp_path):
    store = Storage(str(tmp_path / "f.sqlite3"))
    assert store.get_flag("paused") is False
    store.set_flag("paused", True)
    assert store.get_flag("paused") is True
    store.set_flag("paused", False)
    assert store.get_flag("paused") is False
    store.close()


def test_mark_and_exit_skips_resolution_horizon(tmp_path):
    """Value bets (horizon='resolution') must never be TP/SL-exited —
    they are held to settlement so the Brier gate gets data."""
    import asyncio

    from polybot.config import Settings
    from polybot.paper.engine import PaperEngine

    store = Storage(str(tmp_path / "h.sqlite3"))
    store.insert_position(Position(
        market_id="1", token_id="tok1", question="Q", side="YES", entry_price=0.5,
        model_prob=0.6, edge=0.1, size_usd=5.0, shares=10.0, ts_open=_now(),
        horizon="resolution",
    ))
    engine = PaperEngine(Settings(), store)
    closed = asyncio.run(engine.mark_and_exit())  # no flow positions → no network calls
    assert closed == []
    assert len(store.open_positions()) == 1
    store.close()


def test_open_position_stores_horizon(tmp_path):
    import asyncio

    from polybot.config import Settings
    from polybot.paper.engine import PaperEngine

    store = Storage(str(tmp_path / "hz.sqlite3"))
    engine = PaperEngine(Settings(), store)
    pos, _ = asyncio.run(engine._open_position(
        _market(clobTokenIds='["t0","t1"]'), 0.5, None, 0.7, 0.05, 100.0, {},
        dry_run=False, strategy_name="t", rationale="r", horizon="flow",
    ))
    assert pos is not None and pos.horizon == "flow"
    assert store.open_positions()[0].horizon == "flow"
    store.close()


def test_open_position_group_cap(tmp_path):
    import asyncio

    from polybot.config import Settings
    from polybot.paper.engine import PaperEngine

    store = Storage(str(tmp_path / "e.sqlite3"))
    settings = Settings(
        max_exposure_per_group_usd=3.0, min_stake_usd=1.0, max_position_usd=15.0,
        bankroll_usd=100.0, kelly_fraction=0.25, max_total_exposure_usd=80.0,
    )
    engine = PaperEngine(settings, store)  # defaults to PaperExecutor
    m = _market(negRiskMarketID="0xG")

    ge: dict[str, float] = {}
    pos, used = asyncio.run(engine._open_position(
        m, 0.5, None, 0.7, 0.05, 100.0, ge, dry_run=False, strategy_name="t", rationale="r"))
    assert pos is not None and used > 0 and ge["neg:0xG"] == used
    assert used <= 3.0 + 1e-9  # clamped to the group budget

    # group already full → no position
    pos2, used2 = asyncio.run(engine._open_position(
        m, 0.5, None, 0.7, 0.05, 100.0, {"neg:0xG": 3.0}, dry_run=True, strategy_name="t", rationale="r"))
    assert pos2 is None and used2 == 0.0
    store.close()
