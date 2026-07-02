"""Live executor — real orders on the Polymarket CLOB V2 via py-clob-client-v2.

⚠️  REAL MONEY. Two safety gates:
  1. Only used when config `mode == "live"` AND a private key is present.
  2. Even then, orders are only POSTED when `live_confirm == "I_UNDERSTAND_LIVE_RISK"`.
     Otherwise it runs "dry-live": the intended order is logged, nothing is sent.

Requires `pip install .[live]` (py-clob-client-v2). Every SDK call is guarded so
a failure degrades to "no fill" rather than crashing the runner.

NOTE: py-clob-client-v2 is new (CLOB V2, 2026) and this path could not be tested
against real funds here. Verify signature_type/funder, USDC allowances, and the
order-response fields on your FIRST supervised live order with a tiny size.
"""

from __future__ import annotations

import logging
from typing import Any

from ..config import Settings
from .base import Fill

log = logging.getLogger(__name__)

LIVE_CONFIRM_SENTINEL = "I_UNDERSTAND_LIVE_RISK"


class LiveExecutor:
    mode = "live"

    def __init__(self, settings: Settings, private_key: str, client: Any | None = None) -> None:
        self.s = settings
        self._armed = settings.live_confirm == LIVE_CONFIRM_SENTINEL
        # `client` injection is for tests; production builds it from the SDK.
        self._client = client if client is not None else self._build_client(settings, private_key)

    @property
    def armed(self) -> bool:
        return self._armed

    def _build_client(self, s: Settings, private_key: str) -> Any:
        from py_clob_client_v2 import ClobClient  # lazy import (optional dep)

        kwargs: dict[str, Any] = {
            "host": s.clob_base_url,
            "chain_id": s.clob_chain_id,
            "key": private_key,
        }
        if s.clob_signature_type is not None:
            kwargs["signature_type"] = s.clob_signature_type
        if s.clob_funder:
            kwargs["funder"] = s.clob_funder

        client = ClobClient(**kwargs)
        try:
            client.set_api_creds(client.create_or_derive_api_key())
        except Exception as e:  # noqa: BLE001
            log.warning("could not derive/set CLOB API creds: %s", e)
        return client

    @staticmethod
    def _round_tick(price: float, tick: float) -> float:
        if tick <= 0:
            return price
        return round(round(price / tick) * tick, 6)

    async def buy(self, token_id: str | None, price: float, size_usd: float, tick_size: float) -> Fill | None:
        if not token_id or not (0.0 < price < 1.0) or size_usd <= 0.0:
            return None
        # Hard cap independent of sizing logic.
        if size_usd > self.s.max_position_usd + 1e-6:
            log.error("LIVE buy blocked: $%.2f exceeds max_position $%.2f", size_usd, self.s.max_position_usd)
            return None
        if not self._armed:
            log.warning("[DRY-LIVE] would BUY $%.2f of %s @~%.3f (arm with POLYBOT_LIVE_CONFIRM=%s)",
                        size_usd, token_id[:14], price, LIVE_CONFIRM_SENTINEL)
            return None
        try:
            from py_clob_client_v2 import MarketOrderArgs, OrderType, PartialCreateOrderOptions, Side
            args = MarketOrderArgs(token_id=token_id, amount=round(size_usd, 2), side=Side.BUY)
            opts = PartialCreateOrderOptions(tick_size=str(tick_size))
            resp = self._client.create_and_post_market_order(args, opts, OrderType.FAK)
        except Exception as e:  # noqa: BLE001
            log.error("LIVE buy failed for %s: %s", token_id[:14], e)
            return None
        return self._fill_from_response(resp, price=price, shares=size_usd / price, size_usd=size_usd)

    async def sell(self, token_id: str | None, price: float, shares: float, tick_size: float) -> Fill | None:
        if not token_id or shares <= 0.0:
            return None
        px = self._round_tick(min(max(price, tick_size), 1.0 - tick_size), tick_size)
        if not self._armed:
            log.warning("[DRY-LIVE] would SELL %.2f sh of %s @%.3f", shares, token_id[:14], px)
            return None
        try:
            from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
            args = OrderArgs(token_id=token_id, price=px, size=round(shares, 2), side=Side.SELL)
            opts = PartialCreateOrderOptions(tick_size=str(tick_size))
            resp = self._client.create_and_post_order(args, opts, OrderType.FAK)
        except Exception as e:  # noqa: BLE001
            log.error("LIVE sell failed for %s: %s", token_id[:14], e)
            return None
        return self._fill_from_response(resp, price=px, shares=shares, size_usd=shares * px)

    def _fill_from_response(self, resp: Any, *, price: float, shares: float, size_usd: float) -> Fill:
        """Best-effort fill parse; falls back to the requested price/size.

        TODO(live): confirm the exact response field names against
        py-clob-client-v2 and use the real matched size/price for accounting.
        """
        data = resp if isinstance(resp, dict) else getattr(resp, "__dict__", {}) or {}
        log.info("LIVE order response: %s", str(data)[:300])
        order_id = data.get("orderID") or data.get("order_id") or data.get("id")
        return Fill(price=price, shares=shares, size_usd=size_usd,
                    order_id=str(order_id) if order_id else None)

    async def usdc_balance(self) -> float | None:
        try:
            bal = self._client.get_balance_allowance()  # signature varies by SDK version
        except Exception:  # noqa: BLE001
            return None
        if isinstance(bal, dict):
            raw = bal.get("balance")
            try:
                return float(raw) / 1_000_000 if raw is not None else None  # USDC has 6 decimals
            except (TypeError, ValueError):
                return None
        return None

    async def aclose(self) -> None:
        return None
