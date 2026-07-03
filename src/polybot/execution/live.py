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
        # Limit-FAK at price + slippage cap: bounds the worst fill (an unbounded
        # market order can eat the whole edge on a thin book).
        px = self._round_tick(min(price + self.s.clob_slippage, 1.0 - tick_size), tick_size)
        if not (0.0 < px < 1.0):
            return None
        shares = round(size_usd / px, 2)
        if not self._armed:
            log.warning("[DRY-LIVE] would BUY %.2f sh of %s @<=%.3f (arm with POLYBOT_LIVE_CONFIRM=%s)",
                        shares, token_id[:14], px, LIVE_CONFIRM_SENTINEL)
            return None
        try:
            from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side
            args = OrderArgs(token_id=token_id, price=px, size=shares, side=Side.BUY)
            opts = PartialCreateOrderOptions(tick_size=str(tick_size))
            resp = self._client.create_and_post_order(args, opts, OrderType.FAK)
        except Exception as e:  # noqa: BLE001
            log.error("LIVE buy failed for %s: %s", token_id[:14], e)
            return None
        return self._fill_from_response(resp, side="BUY", price=px, shares=shares, size_usd=shares * px)

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
        return self._fill_from_response(resp, side="SELL", price=px, shares=shares, size_usd=shares * px)

    def _fill_from_response(
        self, resp: Any, *, side: str, price: float, shares: float, size_usd: float
    ) -> Fill | None:
        """Parse the actual matched amounts out of the order response.

        Returning the *requested* size when the FAK didn't (fully) match would
        corrupt the ledger: buys record shares we don't own, sells mark
        positions closed while the tokens are still in the wallet. So:
          * explicit failure / unmatched → None (no fill recorded);
          * matched with amounts → real matched price/size;
          * matched but amounts unparsable → fall back to requested (logged).
        CLOB semantics: BUY makes USDC / takes shares; SELL makes shares / takes USDC.
        """
        data = resp if isinstance(resp, dict) else getattr(resp, "__dict__", {}) or {}
        log.info("LIVE %s response: %s", side, str(data)[:300])
        if data.get("success") is False:
            return None
        status = str(data.get("status") or "").lower()
        if status in ("unmatched", "canceled", "cancelled", "rejected", "invalid"):
            return None
        order_id = data.get("orderID") or data.get("order_id") or data.get("id")
        oid = str(order_id) if order_id else None

        def _f(v: Any) -> float | None:
            try:
                return float(v)
            except (TypeError, ValueError):
                return None

        making, taking = _f(data.get("makingAmount")), _f(data.get("takingAmount"))
        if making is not None and taking is not None and making > 0 and taking > 0:
            if side == "BUY":
                usd, sh = making, taking
            else:
                usd, sh = taking, making
            return Fill(price=usd / sh, shares=sh, size_usd=usd, order_id=oid)

        log.warning("LIVE %s: no matched amounts in response, assuming full fill @%.3f — verify!",
                    side, price)
        return Fill(price=price, shares=shares, size_usd=size_usd, order_id=oid)

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
