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
from .paper.strategy import MicrostructureStrategy, PlaceholderStrategy
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
# strategy factory
# --------------------------------------------------------------------------- #
def _build_strategy(name: str, pull: float | None):
    settings = get_settings()
    if name == "micro":
        strat = MicrostructureStrategy(
            min_imbalance=settings.micro_min_imbalance,
            levels=settings.micro_depth_levels,
            edge_scale=settings.micro_edge_scale,
        )
        return strat, settings.micro_min_edge
    strat = PlaceholderStrategy(pull=settings.placeholder_pull if pull is None else pull)
    return strat, settings.min_edge


# --------------------------------------------------------------------------- #
# paper-tick / mark / resolve / report
# --------------------------------------------------------------------------- #
async def _run_tick(strategy: str, top: int, pull: float | None, min_edge: float | None, dry_run: bool) -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    strat, default_edge = _build_strategy(strategy, pull)
    edge = default_edge if min_edge is None else min_edge
    engine = PaperEngine(settings, store, strat)
    opened = await engine.tick(top_candidates=top, min_edge=edge, dry_run=dry_run)
    log.info("Opened %d paper position(s) via %s%s",
             len(opened), strat.name, " [dry-run]" if dry_run else "")
    for p in opened:
        print(f"  {p.side:<3} entry {p.entry_price:.3f}  ${p.size_usd:5.2f}  "
              f"edge {p.edge:+.3f}  {p.question[:48]}")
    store.close()


async def _run_mark() -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    engine = PaperEngine(settings, store, PlaceholderStrategy())
    closed = await engine.mark_and_exit()
    log.info("Marked-to-market: closed %d position(s)", len(closed))
    for p, pnl, reason in closed:
        print(f"  {p.side:<3} {reason:<11} pnl ${pnl:+6.2f}  {p.question[:48]}")
    store.close()


async def _run_resolve() -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    engine = PaperEngine(settings, store, PlaceholderStrategy())
    closed = await engine.resolve()
    log.info("Resolved/closed %d position(s)", len(closed))
    for p, pnl in closed:
        print(f"  {p.side:<3} pnl ${pnl:+6.2f}  {p.question[:48]}")
    store.close()


def _run_report() -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    positions = store.all_positions()
    report = build_report(positions)

    resolution = report.pop("resolution", None)
    print(json.dumps(report, indent=2))

    if resolution:
        calibration = resolution.pop("calibration", None)
        print("\nresolution (held-to-settle) metrics:")
        print(json.dumps(resolution, indent=2))
        if calibration:
            print("calibration (model prob bucket → predicted vs realized):")
            for row in calibration:
                print(f"  {row['bucket']}  n={row['n']:>3}  "
                      f"pred={row['pred']:.3f}  actual={row['actual']:.3f}")

    open_ = [p for p in positions if p.status == "open"]
    if open_:
        print(f"\nopen positions ({len(open_)}):")
        for p in open_:
            print(f"  #{p.id:<3} {p.side:<3} {p.entry_price:.3f} ${p.size_usd:5.2f}  {p.question[:48]}")
    store.close()


# --------------------------------------------------------------------------- #
# run loop
# --------------------------------------------------------------------------- #
async def _run_loop(strategy: str, top: int, interval: int | None, once: bool, min_edge: float | None) -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    strat, default_edge = _build_strategy(strategy, None)
    edge = default_edge if min_edge is None else min_edge
    interval = settings.runner_interval_seconds if interval is None else interval
    engine = PaperEngine(settings, store, strat)
    log.info("runner start: strategy=%s edge=%.3f interval=%ss%s",
             strat.name, edge, interval, " [once]" if once else "")
    cycle = 0
    try:
        while True:
            cycle += 1
            resolved = await engine.resolve()
            exited = await engine.mark_and_exit()
            opened = await engine.tick(top_candidates=top, min_edge=edge)
            log.info(
                "cycle %d: resolved %d | exited %d | opened %d | open=%d exposure=$%.2f",
                cycle, len(resolved), len(exited), len(opened),
                len(store.open_positions()), store.open_exposure(),
            )
            if once:
                break
            await asyncio.sleep(interval)
    except asyncio.CancelledError:
        pass
    finally:
        store.close()
        log.info("runner stopped after %d cycle(s)", cycle)


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
    tick.add_argument("--strategy", choices=["micro", "placeholder"], default="micro")
    tick.add_argument("--top", type=int, default=30, help="candidates to consider")
    tick.add_argument("--pull", type=float, default=None, help="placeholder strategy strength")
    tick.add_argument("--min-edge", type=float, default=None, help="override min edge")
    tick.add_argument("--dry-run", action="store_true", help="don't persist")

    run = sub.add_parser("run", help="Run the paper loop: resolve → mark → tick on an interval")
    run.add_argument("--strategy", choices=["micro", "placeholder"], default="micro")
    run.add_argument("--top", type=int, default=30)
    run.add_argument("--interval", type=int, default=None, help="seconds between cycles")
    run.add_argument("--once", action="store_true", help="run a single cycle and exit")
    run.add_argument("--min-edge", type=float, default=None)

    sub.add_parser("mark", help="Mark open positions to market; exit on TP/SL/max-hold")
    sub.add_parser("resolve", help="Close positions whose markets have resolved")
    sub.add_parser("report", help="Show ledger metrics (ROI, Brier, calibration)")

    args = parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level)

    cmd = args.cmd or "screen"
    if cmd == "screen":
        asyncio.run(_run_screen(getattr(args, "top", 25), getattr(args, "max_markets", 2000)))
    elif cmd == "paper-tick":
        asyncio.run(_run_tick(args.strategy, args.top, args.pull, args.min_edge, args.dry_run))
    elif cmd == "run":
        try:
            asyncio.run(_run_loop(args.strategy, args.top, args.interval, args.once, args.min_edge))
        except KeyboardInterrupt:
            log.info("interrupted")
    elif cmd == "mark":
        asyncio.run(_run_mark())
    elif cmd == "resolve":
        asyncio.run(_run_resolve())
    elif cmd == "report":
        _run_report()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
