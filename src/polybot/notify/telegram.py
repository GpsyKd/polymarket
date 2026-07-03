"""Minimal Telegram control + notifications (httpx; no extra dependency).

Notifications are pushed to a single owner chat; that same chat can send
commands. `/pause` is a manual kill-switch the runner honours (no new opens
until `/resume`). Only the configured chat id is allowed to issue commands.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from ..paper.metrics import build_report
from ..paper.storage import Storage

log = logging.getLogger(__name__)

HELP = (
    "polybot commands:\n"
    "/status — open positions, exposure, 24h PnL, pause state\n"
    "/positions — list open positions\n"
    "/report — ledger metrics (ROI, Brier)\n"
    "/pause — stop opening new positions (kill-switch)\n"
    "/resume — resume opening\n"
    "/help — this message"
)


@dataclass
class ControlState:
    """Pause flag, persisted via Storage so /pause survives a restart/crash."""

    paused: bool = False
    store: Storage | None = None

    @classmethod
    def load(cls, store: Storage) -> "ControlState":
        return cls(paused=store.get_flag("paused"), store=store)

    def set_paused(self, value: bool) -> None:
        self.paused = value
        if self.store is not None:
            self.store.set_flag("paused", value)


def resolve_bot_token(settings: Any) -> str | None:
    return settings.telegram_bot_token or os.environ.get("TELEGRAM_BOT_TOKEN")


def resolve_chat_id(settings: Any) -> str | None:
    cid = settings.telegram_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
    return str(cid) if cid else None


class TelegramClient:
    def __init__(self, token: str, timeout: float = 35.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=f"https://api.telegram.org/bot{token}", timeout=timeout
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def send_message(self, chat_id: str, text: str) -> None:
        try:
            await self._client.post(
                "/sendMessage",
                json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            )
        except httpx.HTTPError as e:
            log.warning("telegram send failed: %s", e)

    async def get_updates(self, offset: int | None, timeout: int = 30) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset
        r = await self._client.get("/getUpdates", params=params, timeout=timeout + 10)
        r.raise_for_status()
        return r.json().get("result", [])


def handle_command(text: str, control: ControlState, store: Storage) -> str | None:
    """Map a command string to a reply (pure w.r.t. the network; reads storage)."""
    parts = (text or "").strip().split()
    if not parts:
        return None
    cmd = parts[0].lower().split("@")[0]  # tolerate /status@BotName

    if cmd in ("/start", "/help"):
        return HELP
    if cmd == "/pause":
        control.set_paused(True)
        return "⏸ Paused — no new positions will be opened until /resume."
    if cmd == "/resume":
        control.set_paused(False)
        return "▶️ Resumed — opening new positions again."
    if cmd == "/status":
        day_ago = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        return (
            f"paused: {control.paused}\n"
            f"open positions: {len(store.open_positions())}\n"
            f"exposure: ${store.open_exposure():.2f}\n"
            f"24h realized PnL: ${store.realized_pnl_since(day_ago):.2f}"
        )
    if cmd == "/positions":
        rows = store.open_positions()
        if not rows:
            return "no open positions"
        lines = [f"{p.side} {p.entry_price:.3f} ${p.size_usd:.2f} — {p.question[:48]}" for p in rows[:30]]
        return "open positions:\n" + "\n".join(lines)
    if cmd == "/report":
        return "report:\n" + _format_report(build_report(store.all_positions()))
    return "unknown command — /help"


def _format_report(rep: dict[str, Any]) -> str:
    keys = ["n_closed", "n_open", "pnl_usd", "roi", "win_rate", "open_exposure_usd"]
    lines = [f"{k}: {rep[k]}" for k in keys if k in rep]
    res = rep.get("resolution")
    if res:
        lines.append(
            f"resolution n={res['n']} brier={res['brier']} "
            f"vs market {res['brier_baseline_market']} (beats_market={res['beats_market']})"
        )
    by_strategy = rep.get("by_strategy")
    if by_strategy:
        lines.append("by strategy:")
        for name, m in by_strategy.items():
            line = f"• {name}: n={m['n_closed']} pnl=${m['pnl_usd']} roi={m['roi']:+.3f}"
            sres = m.get("resolution")
            if sres:
                line += f" | brier {sres['brier']} vs {sres['brier_baseline_market']}"
            lines.append(line)
    return "\n".join(lines) if lines else "no data yet"


async def command_loop(
    client: TelegramClient, chat_id: str, control: ControlState, store: Storage
) -> None:
    offset: int | None = None
    log.info("telegram command loop started")
    while True:
        try:
            updates = await client.get_updates(offset, timeout=30)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - keep polling through transient errors
            log.warning("telegram getUpdates failed: %s", e)
            await asyncio.sleep(5)
            continue
        for u in updates:
            offset = u["update_id"] + 1
            msg = u.get("message") or u.get("edited_message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(chat_id):
                continue  # ignore everyone except the owner
            reply = handle_command(msg.get("text", ""), control, store)
            if reply:
                await client.send_message(chat_id, reply)
