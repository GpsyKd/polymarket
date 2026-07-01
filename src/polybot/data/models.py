"""Domain models for Polymarket data.

Gamma returns several numeric fields as strings ("14631.82") and several
array fields as JSON-encoded strings ('["Yes","No"]'). The validators below
normalise both into proper Python types.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _to_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            return None
    return None


def _to_list(v: Any) -> list:
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        if not v:
            return []
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


class Market(BaseModel):
    """A single Polymarket market (from the Gamma API)."""

    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    id: str
    question: str
    description: str | None = None
    slug: str | None = None
    condition_id: str | None = Field(default=None, alias="conditionId")

    liquidity: float | None = None
    volume: float | None = None
    volume_24hr: float | None = Field(default=None, alias="volume24hr")

    end_date: datetime | None = Field(default=None, alias="endDate")
    start_date: datetime | None = Field(default=None, alias="startDate")

    active: bool = True
    closed: bool = False

    outcomes: list[str] = Field(default_factory=list)
    outcome_prices: list[float] = Field(default_factory=list, alias="outcomePrices")
    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")

    spread: float | None = None
    best_bid: float | None = Field(default=None, alias="bestBid")
    best_ask: float | None = Field(default=None, alias="bestAsk")

    neg_risk_market_id: str | None = Field(default=None, alias="negRiskMarketID")
    group_item_title: str | None = Field(default=None, alias="groupItemTitle")
    events: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator(
        "liquidity", "volume", "volume_24hr", "spread", "best_bid", "best_ask",
        mode="before",
    )
    @classmethod
    def _v_float(cls, v: Any) -> float | None:
        return _to_float(v)

    @field_validator("outcomes", "clob_token_ids", mode="before")
    @classmethod
    def _v_str_list(cls, v: Any) -> list[str]:
        return [str(x) for x in _to_list(v)]

    @field_validator("outcome_prices", mode="before")
    @classmethod
    def _v_price_list(cls, v: Any) -> list[float]:
        out: list[float] = []
        for x in _to_list(v):
            f = _to_float(x)
            if f is not None:
                out.append(f)
        return out

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2

    def yes_price(self) -> float | None:
        """Price of the first outcome (Polymarket lists 'Yes' first for binary markets)."""
        return self.outcome_prices[0] if self.outcome_prices else None

    def group_key(self) -> str:
        """Correlation key: neg-risk group (mutually-exclusive outcomes) > event > self."""
        if self.neg_risk_market_id:
            return f"neg:{self.neg_risk_market_id}"
        if self.events:
            ident = self.events[0].get("id") or self.events[0].get("slug")
            if ident:
                return f"evt:{ident}"
        return f"mkt:{self.id}"
