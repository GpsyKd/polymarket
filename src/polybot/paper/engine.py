"""Trading engine (paper or live, depending on the injected Executor).

Positions leave the book two ways:
  * resolve()        — the market settled; pay 1/0 on the winning outcome.
  * mark_and_exit()  — short-horizon (flow) exit at the current mid on
                        take-profit / stop-loss / max-hold.

Positions are opened by tick() (a per-market Strategy), llm_tick() (the news+LLM
funnel), or whale_tick() (large-trade flow); all share _open_position() for
sizing, risk caps, and execution.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from ..config import Settings
from ..data.clob import ClobClient, OrderBook
from ..data.gamma import GammaClient
from ..data.models import Market
from ..execution.base import Executor, Fill
from ..execution.paper import PaperExecutor
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


def exit_decision(
    entry_price: float,
    current_price: float,
    ts_open_iso: str,
    now: datetime,
    tp: float,
    sl: float,
    max_hold_hours: float,
    fee: float = 0.0,
) -> tuple[float, str] | None:
    """Decide whether to close a flow position now, using the held token's price.

    `current_price` is the current mid of the token we hold; `fee` models the
    half-spread paid when selling back into the book. Returns (exit_price, reason).
    """
    current = current_price - fee
    move = current - entry_price  # we are long the held token at entry_price
    if move >= tp:
        return current, "take_profit"
    if -move >= sl:
        return current, "stop_loss"
    try:
        opened = datetime.fromisoformat(ts_open_iso)
    except ValueError:
        return None
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    if (now - opened).total_seconds() / 3600.0 >= max_hold_hours:
        return current, "max_hold"
    return None


def _token_for_side(market: Market, side: str) -> str | None:
    """The token we actually hold for a bet: outcome[0] for YES, outcome[1] for NO."""
    tokens = market.clob_token_ids
    if side == "YES":
        return tokens[0] if tokens else None
    return tokens[1] if len(tokens) >= 2 else None


class PaperEngine:
    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        strategy: Strategy | None = None,
        executor: Executor | None = None,
    ) -> None:
        self.s = settings
        self.store = storage
        self.strategy = strategy
        self.executor: Executor = executor or PaperExecutor()

    def _load_state(self) -> tuple[set[str], float, dict[str, float]]:
        open_ids = self.store.open_market_ids()
        remaining = self.s.max_total_exposure_usd - self.store.open_exposure()
        group_exposure = self.store.exposure_by_group()
        return open_ids, remaining, group_exposure

    def _group_full(self, market: Market, group_exposure: dict[str, float]) -> bool:
        left = self.s.max_exposure_per_group_usd - group_exposure.get(market.group_key(), 0.0)
        return left < self.s.min_stake_usd

    async def _open_position(
        self,
        market: Market,
        yes_price: float,
        book: OrderBook | None,
        prob_yes: float,
        edge_floor: float,
        remaining: float,
        group_exposure: dict[str, float],
        dry_run: bool,
        *,
        strategy_name: str,
        rationale: str,
    ) -> tuple[Position | None, float]:
        s = self.s
        gkey = market.group_key()
        remaining_group = s.max_exposure_per_group_usd - group_exposure.get(gkey, 0.0)
        if remaining_group < s.min_stake_usd:
            return None, 0.0

        half_spread = (book.spread / 2.0) if (book and book.spread is not None) else 0.0
        bet = decide_bet(
            prob_yes,
            yes_price,
            bankroll=s.bankroll_usd,
            min_edge=edge_floor,
            kelly_mult=s.kelly_fraction,
            max_position=s.max_position_usd,
            min_stake=s.min_stake_usd,
            remaining_exposure=min(remaining, remaining_group),
            fee=half_spread,
        )
        if bet is None:
            return None, 0.0

        token_id = _token_for_side(market, bet.side)
        tick_size = (book.tick_size if (book and book.tick_size) else s.clob_tick_size)

        # dry-run never touches the executor (so it can never place a real order).
        if dry_run:
            fill: Fill | None = Fill(price=bet.price, shares=bet.shares, size_usd=bet.size_usd)
        else:
            fill = await self.executor.buy(token_id, bet.price, bet.size_usd, tick_size)
        if fill is None or fill.size_usd <= 0.0 or fill.shares <= 0.0:
            return None, 0.0

        pos = Position(
            market_id=market.id,
            token_id=token_id,
            question=market.question,
            side=bet.side,
            entry_price=fill.price,
            model_prob=bet.prob,
            edge=bet.prob - fill.price,
            size_usd=fill.size_usd,
            shares=fill.shares,
            ts_open=_now_iso(),
            strategy=strategy_name,
            rationale=rationale,
            group_key=gkey,
            mode=self.executor.mode,
        )
        if not dry_run:
            pos.id = self.store.insert_position(pos)
        group_exposure[gkey] = group_exposure.get(gkey, 0.0) + fill.size_usd
        return pos, fill.size_usd

    async def _fetch_book(self, clob: ClobClient, market: Market) -> OrderBook | None:
        token = market.clob_token_ids[0] if market.clob_token_ids else None
        if not token:
            return None
        try:
            return await clob.fetch_book(token)
        except Exception as e:  # noqa: BLE001
            log.debug("book fetch failed for %s: %s", token, e)
            return None

    async def tick(
        self, top_candidates: int = 30, min_edge: float | None = None, dry_run: bool = False
    ) -> list[Position]:
        if self.strategy is None:
            raise ValueError("tick() requires a strategy")
        s = self.s
        edge_floor = s.min_edge if min_edge is None else min_edge

        async with GammaClient(s.gamma_base_url, s.http_timeout) as gamma:
            markets = await gamma.fetch_markets(max_markets=2000)
        screened = screen_markets(markets, s)
        log.info("screened %d / %d markets", len(screened), len(markets))

        open_ids, remaining, group_exposure = self._load_state()
        opened: list[Position] = []

        async with ClobClient(s.clob_base_url, s.http_timeout) as clob:
            for r in screened[:top_candidates]:
                if len(opened) >= s.max_new_positions_per_tick or remaining < s.min_stake_usd:
                    break
                m = r.market
                if m.id in open_ids or self._group_full(m, group_exposure):
                    continue

                book = await self._fetch_book(clob, m)
                yes_price = book.mid if (book and book.mid is not None) else m.yes_price()
                if yes_price is None or not (0.0 < yes_price < 1.0):
                    continue
                if book and book.spread is not None and book.spread > s.screen_max_spread:
                    continue

                signal = self.strategy.evaluate(m, yes_price, book)
                if signal is None:
                    continue

                pos, used = await self._open_position(
                    m, yes_price, book, signal.prob_yes, edge_floor, remaining, group_exposure,
                    dry_run, strategy_name=self.strategy.name, rationale=signal.rationale,
                )
                if pos is None:
                    continue
                opened.append(pos)
                remaining -= used
                open_ids.add(m.id)

        return opened

    async def llm_tick(
        self, analyzer, top_candidates: int = 30, min_edge: float | None = None, dry_run: bool = False
    ) -> list[Position]:
        s = self.s
        edge_floor = s.min_edge if min_edge is None else min_edge

        async with GammaClient(s.gamma_base_url, s.http_timeout) as gamma:
            markets = await gamma.fetch_markets(max_markets=2000)
        screened = screen_markets(markets, s)
        candidates = [r.market for r in screened[: max(top_candidates, s.llm_triage_batch)]][: s.llm_triage_batch]

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=s.llm_analysis_ttl_hours)).isoformat()
        recent = self.store.recently_analyzed_ids(cutoff)
        before = len(candidates)
        candidates = [m for m in candidates if m.id not in recent]
        skipped = before - len(candidates)

        selected = await analyzer.triage(candidates, s.llm_max_deep)
        log.info("llm triage: %d fresh candidates -> %d selected (%d skipped, analyzed <%.0fh ago)",
                 len(candidates), len(selected), skipped, s.llm_analysis_ttl_hours)

        open_ids, remaining, group_exposure = self._load_state()
        opened: list[Position] = []

        async with ClobClient(s.clob_base_url, s.http_timeout) as clob:
            for m in selected:
                if len(opened) >= s.max_new_positions_per_tick or remaining < s.min_stake_usd:
                    break
                if m.id in open_ids or self._group_full(m, group_exposure):
                    continue

                book = await self._fetch_book(clob, m)
                yes_price = book.mid if (book and book.mid is not None) else m.yes_price()
                if yes_price is None or not (0.0 < yes_price < 1.0):
                    continue
                if book and book.spread is not None and book.spread > s.screen_max_spread:
                    continue

                signal = await analyzer.deep_analyze(m, yes_price)
                if signal is not None and not dry_run:
                    self.store.record_analysis(
                        m.id, signal.prob_yes, signal.confidence, analyzer.deep_model, _now_iso()
                    )
                if signal is None:
                    continue

                pos, used = await self._open_position(
                    m, yes_price, book, signal.prob_yes, edge_floor, remaining, group_exposure,
                    dry_run, strategy_name=analyzer.name, rationale=signal.rationale,
                )
                if pos is None:
                    continue
                opened.append(pos)
                remaining -= used
                open_ids.add(m.id)

        return opened

    async def whale_tick(
        self, analyzer, top_candidates: int = 30, min_edge: float | None = None, dry_run: bool = False
    ) -> list[Position]:
        s = self.s
        edge_floor = s.min_edge if min_edge is None else min_edge

        async with GammaClient(s.gamma_base_url, s.http_timeout) as gamma:
            markets = await gamma.fetch_markets(max_markets=2000)
        screened = screen_markets(markets, s)
        log.info("whale: scanning up to %d candidates", min(top_candidates, len(screened)))

        open_ids, remaining, group_exposure = self._load_state()
        opened: list[Position] = []

        async with ClobClient(s.clob_base_url, s.http_timeout) as clob:
            for r in screened[:top_candidates]:
                if len(opened) >= s.max_new_positions_per_tick or remaining < s.min_stake_usd:
                    break
                m = r.market
                if m.id in open_ids or self._group_full(m, group_exposure) or not m.condition_id:
                    continue

                book = await self._fetch_book(clob, m)
                yes_price = book.mid if (book and book.mid is not None) else m.yes_price()
                if yes_price is None or not (0.0 < yes_price < 1.0):
                    continue
                if book and book.spread is not None and book.spread > s.screen_max_spread:
                    continue

                signal = await analyzer.analyze(m, yes_price)
                if signal is None:
                    continue

                pos, used = await self._open_position(
                    m, yes_price, book, signal.prob_yes, edge_floor, remaining, group_exposure,
                    dry_run, strategy_name=analyzer.name, rationale=signal.rationale,
                )
                if pos is None:
                    continue
                opened.append(pos)
                remaining -= used
                open_ids.add(m.id)

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
                    self.store.close_position(p.id, exit_price, pnl, winner, _now_iso(), "resolution")
                closed.append((p, pnl))
        return closed

    async def mark_and_exit(self) -> list[tuple[Position, float, str]]:
        s = self.s
        now = datetime.now(timezone.utc)
        closed: list[tuple[Position, float, str]] = []
        async with ClobClient(s.clob_base_url, s.http_timeout) as clob:
            for p in self.store.open_positions():
                if not p.token_id:
                    continue
                try:
                    book = await clob.fetch_book(p.token_id)
                except Exception as e:  # noqa: BLE001
                    log.debug("mark book fetch failed for %s: %s", p.token_id, e)
                    continue
                held_mid = book.mid if book else None
                if held_mid is None:
                    continue
                half = (book.spread / 2.0) if (book and book.spread is not None) else 0.0
                decision = exit_decision(
                    p.entry_price, held_mid, p.ts_open, now,
                    s.exit_take_profit, s.exit_stop_loss, s.exit_max_hold_hours, fee=half,
                )
                if decision is None:
                    continue
                target_price, reason = decision

                tick_size = book.tick_size if (book and book.tick_size) else s.clob_tick_size
                fill = await self.executor.sell(p.token_id, target_price, p.shares, tick_size)
                if fill is None:
                    continue
                pnl = fill.shares * fill.price - p.size_usd
                if p.id is not None:
                    self.store.close_position(
                        p.id, fill.price, pnl, f"exit@{fill.price:.3f}", _now_iso(), reason
                    )
                closed.append((p, pnl, reason))
        return closed
