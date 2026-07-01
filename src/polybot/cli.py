"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from .config import get_settings
from .data.gamma import GammaClient
from .logsetup import setup_logging
from .paper.engine import PaperEngine
from .paper.metrics import build_report
from .paper.storage import Storage
from .paper.strategy import PlaceholderStrategy
from .screener.stage0 import ScreenResult, screen_markets

log = logging.getLogger("polybot")


# --------------------------------------------------------------------------- #
# screen
# --------------------------------------------------------------------------- #
async def _run_screen(top: int, max_markets: int) -> None:
    settings = get_settings()
    async with GammaClient(settings.gamma_base_url, timeout=settings.http_timeout) as gamma:
        log.info("Fetching markets from Gamma (up to %d)…", max_markets)
        markets = await gamma.fetch_markets(max_markets=max_markets)
    log.info("Fetched %d markets", len(markets))

    results = screen_markets(markets, settings)
    log.info("Stage-0 passed: %d / %d markets", len(results), len(markets))
    print()
    _print_table(results[:top])


def _print_table(results: list[ScreenResult]) -> None:
    header = f"{'LIQ $':>10}  {'24h $':>10}  {'YES':>4}  {'d→res':>6}  QUESTION"
    print(header)
    print("-" * max(len(header), 80))
    for r in results:
        m = r.market
        liq = m.liquidity or 0.0
        v24 = m.volume_24hr or 0.0
        yes = m.yes_price()
        yes_s = f"{yes:.2f}" if yes is not None else "  - "
        days = (r.hours_to_resolve or 0.0) / 24.0
        q = m.question if len(m.question) <= 58 else m.question[:57] + "…"
        print(f"{liq:>10,.0f}  {v24:>10,.0f}  {yes_s:>4}  {days:>6.1f}  {q}")


# --------------------------------------------------------------------------- #
# paper-tick / resolve / report
# --------------------------------------------------------------------------- #
async def _run_tick(top: int, pull: float | None, min_edge: float | None, dry_run: bool) -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    strat = PlaceholderStrategy(pull=settings.placeholder_pull if pull is None else pull)
    engine = PaperEngine(settings, store, strat)
    opened = await engine.tick(top_candidates=top, min_edge=min_edge, dry_run=dry_run)
    log.info("Opened %d paper position(s)%s", len(opened), " [dry-run]" if dry_run else "")
    for p in opened:
        q = p.question[:50]
        print(f"  {p.side:<3} entry {p.entry_price:.3f}  ${p.size_usd:5.2f}  edge {p.edge:+.3f}  {q}")
    store.close()


async def _run_resolve() -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    engine = PaperEngine(settings, store, PlaceholderStrategy())
    closed = await engine.resolve()
    log.info("Resolved/closed %d position(s)", len(closed))
    for p, pnl in closed:
        print(f"  {p.side:<3} pnl ${pnl:+6.2f}  {p.question[:50]}")
    store.close()


def _run_report() -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    positions = store.all_positions()
    report = build_report(positions)

    calibration = report.pop("calibration", None)
    print(json.dumps(report, indent=2))

    if calibration:
        print("\ncalibration (model prob bucket → predicted vs realized win rate):")
        for row in calibration:
            print(f"  {row['bucket']}  n={row['n']:>3}  pred={row['pred']:.3f}  actual={row['actual']:.3f}")

    open_ = [p for p in positions if p.status == "open"]
    if open_:
        print(f"\nopen positions ({len(open_)}):")
        for p in open_:
            print(f"  #{p.id:<3} {p.side:<3} {p.entry_price:.3f} ${p.size_usd:5.2f}  {p.question[:50]}")
    store.close()


# --------------------------------------------------------------------------- #
# entrypoint
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(prog="polybot", description="Polymarket auto-betting bot")
    sub = parser.add_subparsers(dest="cmd")

    screen = sub.add_parser("screen", help="Fetch and Stage-0 screen markets")
    screen.add_argument("--top", type=int, default=25)
    screen.add_argument("--max-markets", type=int, default=2000)

    tick = sub.add_parser("paper-tick", help="Screen, decide, and open paper positions")
    tick.add_argument("--top", type=int, default=30, help="candidates to consider")
    tick.add_argument("--pull", type=float, default=None, help="placeholder strategy strength")
    tick.add_argument("--min-edge", type=float, default=None, help="override min edge")
    tick.add_argument("--dry-run", action="store_true", help="don't persist")

    sub.add_parser("resolve", help="Close paper positions whose markets have resolved")
    sub.add_parser("report", help="Show ledger metrics (ROI, Brier, calibration)")

    args = parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level)

    cmd = args.cmd or "screen"
    if cmd == "screen":
        asyncio.run(_run_screen(getattr(args, "top", 25), getattr(args, "max_markets", 2000)))
    elif cmd == "paper-tick":
        asyncio.run(_run_tick(args.top, args.pull, args.min_edge, args.dry_run))
    elif cmd == "resolve":
        asyncio.run(_run_resolve())
    elif cmd == "report":
        _run_report()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
