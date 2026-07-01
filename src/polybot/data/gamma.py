"""Client for the Polymarket Gamma API (market metadata)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .models import Market

log = logging.getLogger(__name__)


class GammaClient:
    def __init__(self, base_url: str, timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            headers={"User-Agent": "polybot/0.1"},
        )

    async def __aenter__(self) -> "GammaClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_markets_page(
        self,
        *,
        limit: int,
        offset: int,
        active: bool,
        closed: bool,
        order: str | None,
        ascending: bool,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
            "closed": str(closed).lower(),
        }
        if order:
            params["order"] = order
            params["ascending"] = str(ascending).lower()
        r = await self._client.get("/markets", params=params)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else []

    async def fetch_markets(
        self,
        *,
        active: bool = True,
        closed: bool = False,
        order: str | None = "volume24hr",
        ascending: bool = False,
        page_size: int = 100,
        max_markets: int = 2000,
    ) -> list[Market]:
        """Fetch markets, paginating until `max_markets` or the feed is exhausted."""
        # Gamma caps a page at 100 regardless of the requested limit.
        page_size = min(page_size, 100)
        markets: list[Market] = []
        offset = 0
        while len(markets) < max_markets:
            try:
                page = await self._get_markets_page(
                    limit=page_size,
                    offset=offset,
                    active=active,
                    closed=closed,
                    order=order,
                    ascending=ascending,
                )
            except httpx.HTTPStatusError as e:
                if order is not None:
                    log.warning("markets request failed with order=%r (%s); retrying unordered",
                                order, e.response.status_code)
                    order = None
                    continue
                raise
            if not page:
                break
            for raw in page:
                try:
                    markets.append(Market.model_validate(raw))
                except Exception as e:  # noqa: BLE001 - skip malformed rows, keep going
                    log.debug("skipping market parse error: %s", e)
            if len(page) < page_size:
                break
            offset += page_size
        return markets[:max_markets]

    async def get_market(self, market_id: str) -> Market | None:
        """Fetch a single market by id (used to detect resolution)."""
        try:
            r = await self._client.get(f"/markets/{market_id}")
            r.raise_for_status()
        except httpx.HTTPStatusError:
            return None
        data = r.json()
        if isinstance(data, list):
            data = data[0] if data else None
        if not data:
            return None
        try:
            return Market.model_validate(data)
        except Exception:  # noqa: BLE001
            return None
