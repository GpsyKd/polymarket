"""Command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from .config import get_settings
from .data.dataapi import PolymarketDataClient
from .data.gamma import GammaClient
from .llm.client import LLMClient, resolve_api_key
from .llm.news_signal import NewsLLMAnalyzer
from .logsetup import setup_logging
from .notify.telegram import (
    ControlState,
    TelegramClient,
    command_loop,
    resolve_bot_token,
    resolve_chat_id,
)
from .whale.analyzer import WhaleAnalyzer
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
# strategy / analyzer factories
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


def _make_llm_client() -> LLMClient | None:
    settings = get_settings()
    key = resolve_api_key(settings)
    if not key:
        log.error("No LLM API key. Set POLYBOT_GROK_API_KEY (or XAI_API_KEY / GROK_API_KEY).")
        return None
    return LLMClient(settings.llm_base_url, key, timeout=settings.http_timeout * 4)


def _make_analyzer(client: LLMClient) -> NewsLLMAnalyzer:
    s = get_settings()
    return NewsLLMAnalyzer(client, s.llm_triage_model, s.llm_deep_model, s.llm_live_search)


def _make_whale_analyzer(dc: PolymarketDataClient) -> WhaleAnalyzer:
    s = get_settings()
    return WhaleAnalyzer(
        dc, s.whale_min_usd, s.whale_edge_scale, s.whale_min_flow,
        s.whale_trades_limit, s.whale_max_age_minutes,
    )


def _build_executor(settings):
    """Paper executor unless mode=live with a key; live is dry until explicitly armed."""
    from .execution.paper import PaperExecutor
    if settings.mode != "live":
        return PaperExecutor()
    import os
    key = settings.polygon_private_key or os.environ.get("POLYGON_PRIVATE_KEY")
    if not key:
        log.error("mode=live but no POLYGON_PRIVATE_KEY — falling back to paper executor")
        return PaperExecutor()
    try:
        from .execution.live import LIVE_CONFIRM_SENTINEL, LiveExecutor
        executor = LiveExecutor(settings, private_key=key)
    except Exception as e:  # noqa: BLE001
        log.error("failed to init live executor (%s) — falling back to paper", e)
        return PaperExecutor()
    if executor.armed:
        log.warning("⚠️  LIVE TRADING ARMED — real orders will be placed with real funds.")
    else:
        log.warning("LIVE mode is DRY (orders logged, not posted). Arm with POLYBOT_LIVE_CONFIRM=%s.",
                    LIVE_CONFIRM_SENTINEL)
    return executor


# --------------------------------------------------------------------------- #
# open / mark / resolve / report
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
    _print_opened(opened)
    store.close()


async def _run_llm_tick(top: int, min_edge: float | None, dry_run: bool) -> None:
    settings = get_settings()
    client = _make_llm_client()
    if client is None:
        return
    store = Storage(settings.db_path)
    edge = settings.min_edge if min_edge is None else min_edge
    async with client:
        analyzer = _make_analyzer(client)
        engine = PaperEngine(settings, store, strategy=None)
        opened = await engine.llm_tick(analyzer, top_candidates=top, min_edge=edge, dry_run=dry_run)
    log.info("llm-tick opened %d position(s)%s", len(opened), " [dry-run]" if dry_run else "")
    _print_opened(opened, show_rationale=True)
    store.close()


async def _run_whale_tick(top: int, min_edge: float | None, dry_run: bool) -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    edge = settings.whale_min_edge if min_edge is None else min_edge
    async with PolymarketDataClient(settings.data_api_base_url, settings.http_timeout) as dc:
        analyzer = _make_whale_analyzer(dc)
        engine = PaperEngine(settings, store)
        opened = await engine.whale_tick(analyzer, top_candidates=top, min_edge=edge, dry_run=dry_run)
    log.info("whale-tick opened %d position(s)%s", len(opened), " [dry-run]" if dry_run else "")
    _print_opened(opened, show_rationale=True)
    store.close()


def _print_opened(opened, show_rationale: bool = False) -> None:
    for p in opened:
        extra = f"  {p.rationale[:44]}" if show_rationale else ""
        print(f"  {p.side:<3} entry {p.entry_price:.3f}  ${p.size_usd:5.2f}  "
              f"edge {p.edge:+.3f}  {p.question[:44]}{extra}")


def _cycle_message(resolved, exited, opened) -> str:
    parts = []
    for p, pnl in resolved:
        parts.append(f"✅ resolved {p.side} ${pnl:+.2f} — {p.question[:40]}")
    for p, pnl, reason in exited:
        parts.append(f"↩️ {reason} {p.side} ${pnl:+.2f} — {p.question[:40]}")
    for p in opened:
        parts.append(f"➕ {p.side} {p.entry_price:.3f} ${p.size_usd:.2f} "
                     f"edge {p.edge:+.3f} — {p.question[:40]}")
    return "\n".join(parts) if parts else "cycle: no changes"


async def _run_mark() -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    engine = PaperEngine(settings, store)
    closed = await engine.mark_and_exit()
    log.info("Marked-to-market: closed %d position(s)", len(closed))
    for p, pnl, reason in closed:
        print(f"  {p.side:<3} {reason:<11} pnl ${pnl:+6.2f}  {p.question[:48]}")
    store.close()


async def _run_resolve() -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    engine = PaperEngine(settings, store)
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

    by_strategy = report.pop("by_strategy", None)
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

    if by_strategy:
        print("\nby strategy (which signal actually has edge):")
        for name, m in by_strategy.items():
            line = (f"  {name:<22} n={m['n_closed']:>3}  pnl=${m['pnl_usd']:>8.2f}  "
                    f"roi={m['roi']:+.3f}  win={m['win_rate']:.2f}")
            res = m.get("resolution")
            if res:
                line += (f"  | resolved={res['n']} brier={res['brier']} "
                         f"vs {res['brier_baseline_market']} beats={res['beats_market']}")
            print(line)

    open_ = [p for p in positions if p.status == "open"]
    if open_:
        print(f"\nopen positions ({len(open_)}):")
        for p in open_:
            print(f"  #{p.id:<3} {p.side:<3} {p.entry_price:.3f} ${p.size_usd:5.2f}  {p.question[:48]}")
    store.close()


async def _run_balance() -> None:
    settings = get_settings()
    executor = _build_executor(settings)
    bal = await executor.usdc_balance()
    print(f"executor mode: {executor.mode}")
    print(f"USDC balance: {'n/a (paper)' if bal is None else f'${bal:.2f}'}")
    await executor.aclose()


# --------------------------------------------------------------------------- #
# run loop
# --------------------------------------------------------------------------- #
def _build_run_context(strategy, settings, store, executor, top, min_edge):
    """Build one ticker per strategy (each with its own interval) + clients to close.

    `strategy` is a single signal name or "all". Every ticker shares the same
    store + executor; resolve/mark/kill-switch run once per cycle over all of them.
    """
    names = ["micro", "llm", "whale"] if strategy == "all" else [strategy]
    tickers: list[dict] = []
    closers: list = []

    for name in names:
        if name == "llm":
            client = _make_llm_client()
            if client is None:
                if strategy == "all":
                    log.warning("skipping llm signal (no API key)")
                    continue
                return [], []
            closers.append(client)
            analyzer = _make_analyzer(client)
            engine = PaperEngine(settings, store, strategy=None, executor=executor)
            edge = settings.min_edge if min_edge is None else min_edge

            def _mk(engine=engine, analyzer=analyzer, edge=edge):
                async def do_tick():
                    return await engine.llm_tick(analyzer, top_candidates=top, min_edge=edge)
                return do_tick
            tickers.append({"name": analyzer.name, "interval": settings.llm_interval_seconds, "do_tick": _mk()})
        elif name == "whale":
            dc = PolymarketDataClient(settings.data_api_base_url, settings.http_timeout)
            closers.append(dc)
            analyzer = _make_whale_analyzer(dc)
            engine = PaperEngine(settings, store, executor=executor)
            edge = settings.whale_min_edge if min_edge is None else min_edge

            def _mk(engine=engine, analyzer=analyzer, edge=edge):
                async def do_tick():
                    return await engine.whale_tick(analyzer, top_candidates=top, min_edge=edge)
                return do_tick
            tickers.append({"name": analyzer.name, "interval": settings.runner_interval_seconds, "do_tick": _mk()})
        else:
            strat, default_edge = _build_strategy(name, None)
            engine = PaperEngine(settings, store, strat, executor=executor)
            edge = default_edge if min_edge is None else min_edge

            def _mk(engine=engine, edge=edge):
                async def do_tick():
                    return await engine.tick(top_candidates=top, min_edge=edge)
                return do_tick
            tickers.append({"name": strat.name, "interval": settings.runner_interval_seconds, "do_tick": _mk()})

    return tickers, closers


async def _run_loop(strategy: str, top: int, interval: int | None, once: bool, min_edge: float | None) -> None:
    settings = get_settings()
    store = Storage(settings.db_path)
    executor = _build_executor(settings)

    tickers, closers = _build_run_context(strategy, settings, store, executor, top, min_edge)
    if not tickers:
        await executor.aclose()
        store.close()
        return
    if interval is not None:  # explicit --interval overrides every strategy's cadence
        for t in tickers:
            t["interval"] = interval

    maint = PaperEngine(settings, store, executor=executor)  # shared resolve/mark/unrealized
    strat_names = "+".join(t["name"] for t in tickers)
    # Wake at least every 5 min for maintenance / pause / kill-switch, but each
    # strategy still ticks only on its own (possibly longer) interval.
    base = max(60, min(300, min(t["interval"] for t in tickers)))

    control = ControlState.load(store)  # /pause survives restarts
    tg_token, tg_chat = resolve_bot_token(settings), resolve_chat_id(settings)
    tg = TelegramClient(tg_token) if (tg_token and tg_chat and not once) else None

    async def notify(text: str) -> None:
        if tg:
            await tg.send_message(tg_chat, text)

    log.info("runner start: strategies=%s mode=%s base=%ss%s telegram=%s paused=%s",
             strat_names, executor.mode, base, " [once]" if once else "", bool(tg), control.paused)

    async def cycle_loop() -> None:
        last: dict[str, float] = {t["name"]: -1e9 for t in tickers}  # all due on cycle 1
        cycle = 0
        kill_active = False
        await notify(f"▶️ polybot started — [{strat_names}], mode={executor.mode}"
                     + (" [paused]" if control.paused else ""))
        try:
            while True:
                cycle += 1
                # One flaky network call must not take the 24/7 runner down.
                try:
                    now = time.monotonic()
                    resolved = await maint.resolve()
                    exited = await maint.mark_and_exit()

                    day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
                    realized = store.realized_pnl_since(day_ago, mode=executor.mode)
                    unrealized = await maint.unrealized_pnl()
                    total_pnl = realized + unrealized

                    opened: list = []
                    if control.paused:
                        pass
                    elif total_pnl <= -settings.daily_loss_limit_usd:
                        log.warning("KILL-SWITCH: PnL $%.2f (24h real $%.2f + open $%.2f) <= -$%.2f — no new opens",
                                    total_pnl, realized, unrealized, settings.daily_loss_limit_usd)
                        if not kill_active:
                            kill_active = True
                            await notify(f"🛑 kill-switch: PnL ${total_pnl:.2f} (open ${unrealized:.2f}) — opens halted")
                    else:
                        if kill_active:
                            kill_active = False
                            await notify("✅ kill-switch cleared — opens resumed")
                        for t in tickers:
                            if now - last[t["name"]] >= t["interval"]:
                                last[t["name"]] = now
                                opened += await t["do_tick"]()

                    log.info(
                        "cycle %d: resolved %d | exited %d | opened %d | open=%d exposure=$%.2f "
                        "| pnl 24h=$%.2f open=$%.2f%s",
                        cycle, len(resolved), len(exited), len(opened),
                        len(store.open_positions(mode=executor.mode)),
                        store.open_exposure(mode=executor.mode), realized, unrealized,
                        " [paused]" if control.paused else "",
                    )
                    if tg and (opened or exited or resolved):
                        await notify(_cycle_message(resolved, exited, opened))
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    log.exception("cycle %d failed; retrying next interval", cycle)
                if once:
                    break
                await asyncio.sleep(base)
        except asyncio.CancelledError:
            pass

    try:
        if tg:
            await asyncio.gather(cycle_loop(), command_loop(tg, tg_chat, control, store))
        else:
            await cycle_loop()
    finally:
        for c in closers:
            await c.aclose()
        if tg is not None:
            await tg.aclose()
        await executor.aclose()
        store.close()
        log.info("runner stopped")


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
    tick.add_argument("--top", type=int, default=30)
    tick.add_argument("--pull", type=float, default=None, help="placeholder strategy strength")
    tick.add_argument("--min-edge", type=float, default=None)
    tick.add_argument("--dry-run", action="store_true")

    llm = sub.add_parser("llm-tick", help="News+LLM funnel: triage → deep-analyze → open")
    llm.add_argument("--top", type=int, default=30, help="candidates fed to triage")
    llm.add_argument("--min-edge", type=float, default=None)
    llm.add_argument("--dry-run", action="store_true")

    whale = sub.add_parser("whale-tick", help="Whale-flow: large-trade lean → open")
    whale.add_argument("--top", type=int, default=30)
    whale.add_argument("--min-edge", type=float, default=None)
    whale.add_argument("--dry-run", action="store_true")

    run = sub.add_parser("run", help="Run the paper loop: resolve → mark → tick on an interval")
    run.add_argument("--strategy", choices=["micro", "placeholder", "llm", "whale", "all"],
                     default="micro", help="'all' runs every signal in one process, each on its own interval")
    run.add_argument("--top", type=int, default=30)
    run.add_argument("--interval", type=int, default=None, help="seconds between cycles")
    run.add_argument("--once", action="store_true", help="run a single cycle and exit")
    run.add_argument("--min-edge", type=float, default=None)

    sub.add_parser("mark", help="Mark open positions to market; exit on TP/SL/max-hold")
    sub.add_parser("resolve", help="Close positions whose markets have resolved")
    sub.add_parser("report", help="Show ledger metrics (ROI, Brier, calibration)")
    sub.add_parser("balance", help="Show executor mode and USDC balance (live)")

    args = parser.parse_args()
    settings = get_settings()
    setup_logging(settings.log_level)

    cmd = args.cmd or "screen"
    if cmd == "screen":
        asyncio.run(_run_screen(getattr(args, "top", 25), getattr(args, "max_markets", 2000)))
    elif cmd == "paper-tick":
        asyncio.run(_run_tick(args.strategy, args.top, args.pull, args.min_edge, args.dry_run))
    elif cmd == "llm-tick":
        asyncio.run(_run_llm_tick(args.top, args.min_edge, args.dry_run))
    elif cmd == "whale-tick":
        asyncio.run(_run_whale_tick(args.top, args.min_edge, args.dry_run))
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
    elif cmd == "balance":
        asyncio.run(_run_balance())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
