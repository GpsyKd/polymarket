"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import logging

from .config import get_settings
from .data.gamma import GammaClient
from .logsetup import setup_logging
from .screener.stage0 import ScreenResult, screen_markets

log = logging.getLogger("polybot")


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


def main() -> None:
    parser = argparse.ArgumentParser(prog="polybot", description="Polymarket auto-betting bot")
    sub = parser.add_subparsers(dest="cmd")

    screen = sub.add_parser("screen", help="Fetch and Stage-0 screen Polymarket markets")
    screen.add_argument("--top", type=int, default=25, help="rows to print")
    screen.add_argument("--max-markets", type=int, default=2000, help="markets to fetch")

    args = parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level)

    cmd = args.cmd or "screen"
    if cmd == "screen":
        top = getattr(args, "top", 25)
        max_markets = getattr(args, "max_markets", 2000)
        asyncio.run(_run_screen(top, max_markets))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
