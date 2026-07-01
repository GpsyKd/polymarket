"""Paper-trading engine: screen -> decide -> open, and resolve -> close."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import Settings
from ..data.clob import ClobClient
from ..data.gamma import GammaClient
from ..data.models import Market
from ..screener.stage0 import screen_markets
from .sizing import decide_bet
from .storage import Position, Storage
from .strategy import Strategy

log = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def winner_label(market: Market) -> str | None:
    """Return the winning outcome label if the market has definitively resolved."""
    if not market.closed:
        return None
    prices, outcomes = market.outcome_prices, market.outcomes
    if not prices or len(prices) != len(outcomes):
        return None
    idx = max(range(len(prices)), key=lambda i: prices[i])
    if prices[idx] < 0.99:
        return None
    return outcomes[idx]


def settle(
    side: str, shares: float, size_usd: float, winner: str, outcomes: list[str]
) -> tuple[float, float, bool]:
    """Settle a resolved binary position. Returns (exit_price, pnl, side_won)."""
    yes_label = outcomes[0] if outcomes else "Yes"
    yes_won = winner == yes_label
    side_won = yes_won if side == "YES" else not yes_won
    exit_price = 1.0 if side_won else 0.0
    pnl = shares * exit_price - size_usd
    return exit_price, pnl, side_won


class PaperEngine:
    def __init__(self, settings: Settings, storage: Storage, strategy: Strategy) -> None:
        self.s = settings
        self.store = storage
        self.strategy = strategy

    async def tick(
        self, top_candidates: int = 30, min_edge: float | None = None, dry_run: bool = False
    ) -> list[Position]:
        s = self.s
        edge_floor = s.min_edge if min_edge is None else min_edge

        async with GammaClient(s.gamma_base_url, s.http_timeout) as gamma:
            markets = await gamma.fetch_markets(max_markets=2000)
        screened = screen_markets(markets, s)
        log.info("screened %d / %d markets", len(screened), len(markets))

        open_ids = self.store.open_market_ids()
        remaining = s.max_total_exposure_usd - self.store.open_exposure()
        opened: list[Position] = []

        async with ClobClient(s.clob_base_url, s.http_timeout) as clob:
            for r in screened[:top_candidates]:
                if len(opened) >= s.max_new_positions_per_tick or remaining < s.min_stake_usd:
                    break
                m = r.market
                if m.id in open_ids:
                    continue

                token = m.clob_token_ids[0] if m.clob_token_ids else None
                book = None
                if token:
                    try:
                        book = await clob.fetch_book(token)
                    except Exception as e:  # noqa: BLE001
                        log.debug("book fetch failed for %s: %s", token, e)

                yes_price = book.mid if (book and book.mid is not None) else m.yes_price()
                if yes_price is None or not (0.0 < yes_price < 1.0):
                    continue
                if book and book.spread is not None and book.spread > s.screen_max_spread:
                    continue

                signal = self.strategy.evaluate(m, yes_price, book)
                if signal is None:
                    continue

                bet = decide_bet(
                    signal.prob_yes,
                    yes_price,
                    bankroll=s.bankroll_usd,
                    min_edge=edge_floor,
                    kelly_mult=s.kelly_fraction,
                    max_position=s.max_position_usd,
                    min_stake=s.min_stake_usd,
                    remaining_exposure=remaining,
                )
                if bet is None:
                    continue

                pos = Position(
                    market_id=m.id,
                    token_id=token,
                    question=m.question,
                    side=bet.side,
                    entry_price=bet.price,
                    model_prob=bet.prob,
                    edge=bet.edge,
                    size_usd=bet.size_usd,
                    shares=bet.shares,
                    ts_open=_now_iso(),
                    strategy=self.strategy.name,
                    rationale=signal.rationale,
                    mode="paper",
                )
                if not dry_run:
                    pos.id = self.store.insert_position(pos)
                opened.append(pos)
                remaining -= bet.size_usd

        return opened

    async def resolve(self) -> list[tuple[Position, float]]:
        closed: list[tuple[Position, float]] = []
        async with GammaClient(self.s.gamma_base_url, self.s.http_timeout) as gamma:
            for p in self.store.open_positions():
                market = await gamma.get_market(p.market_id)
                if market is None:
                    continue
                winner = winner_label(market)
                if winner is None:
                    continue
                exit_price, pnl, _ = settle(p.side, p.shares, p.size_usd, winner, market.outcomes)
                if p.id is not None:
                    self.store.close_position(p.id, exit_price, pnl, winner, _now_iso())
                closed.append((p, pnl))
        return closed
