"""News + LLM signal: a two-stage funnel over screened markets.

  Stage 1 (triage, cheap model): pick the handful of markets where fresh public
    information could plausibly reveal a mispricing worth a costly deep dive.
  Stage 2 (deep, strong model + live search): estimate P(YES) with a rationale
    and a self-reported confidence.

This is a *value* signal (held to resolution, graded by Brier vs the market).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from ..data.models import Market
from ..paper.strategy import Signal
from .client import LLMClient

log = logging.getLogger(__name__)

TRIAGE_SYSTEM = (
    "You are a triage filter for a prediction-market forecasting bot with a limited "
    "analysis budget. You receive a list of markets (id, question, current YES price "
    "0-1, days to resolution). Select ONLY the markets where fresh public information "
    "(recent news, events, announcements, sentiment) could plausibly reveal that the "
    "current price is mispriced and so is worth a costly deep analysis. Prefer markets "
    "that are news-sensitive and time-relevant. Avoid near-certain outcomes (price ~0 "
    "or ~1), pure chance with no informational edge, and questions needing non-public "
    "information. Respond ONLY with JSON: {\"selected\": [\"<id>\", ...]} with at most "
    "`select_up_to` ids, best first."
)

DEEP_SYSTEM = (
    "You are a careful, calibrated forecaster analyzing ONE prediction market. Use the "
    "latest available information to estimate the probability it resolves YES. Consider "
    "base rates, recent news, and the resolution criteria. Be honest about uncertainty: "
    "if you have no real edge over the market's current price, set confidence low. Do "
    "NOT just anchor to the current price. Respond ONLY with JSON: {\"prob_yes\": "
    "<0..1>, \"confidence\": <0..1>, \"rationale\": \"<=40 words\"}. `confidence` is how "
    "much you trust your estimate over the market price."
)


def _days_to_resolve(market: Market, now: datetime) -> float:
    if market.end_date is None:
        return 0.0
    return (market.end_date - now).total_seconds() / 86400.0


class NewsLLMAnalyzer:
    name = "news-llm"
    horizon = "resolution"

    def __init__(
        self,
        client: LLMClient,
        triage_model: str,
        deep_model: str,
        live_search: bool = True,
    ) -> None:
        self.client = client
        self.triage_model = triage_model
        self.deep_model = deep_model
        self.live_search = live_search

    async def triage(self, markets: list[Market], limit: int) -> list[Market]:
        if not markets:
            return []
        now = datetime.now(timezone.utc)
        payload = [
            {
                "id": m.id,
                "question": m.question[:160],
                "yes_price": round(m.yes_price() or 0.0, 3),
                "days_to_resolve": round(_days_to_resolve(m, now), 1),
            }
            for m in markets
        ]
        data = await self.client.complete_json(
            TRIAGE_SYSTEM,
            json.dumps({"select_up_to": limit, "markets": payload}),
            self.triage_model,
        )
        if not data:
            return []
        chosen_ids = {str(x) for x in (data.get("selected") or [])}
        return [m for m in markets if m.id in chosen_ids][:limit]

    async def deep_analyze(self, market: Market, yes_price: float) -> Signal | None:
        """Returns a Signal for every successfully parsed analysis, including
        low-confidence ones — the engine records it (for TTL dedup / research)
        and applies the confidence gate itself. None only on API/parse failure."""
        now = datetime.now(timezone.utc)
        user = json.dumps({
            "question": market.question,
            "resolution_criteria": (market.description or "")[:1200],
            "current_yes_price": round(yes_price, 3),
            "days_to_resolve": round(_days_to_resolve(market, now), 1),
            "volume_24h_usd": round(market.volume_24hr or 0.0),
            "liquidity_usd": round(market.liquidity or 0.0),
            "price_change_24h": market.price_change_24h,
            "price_change_1h": market.price_change_1h,
        })
        data = await self.client.complete_json(
            DEEP_SYSTEM, user, self.deep_model, live_search=self.live_search
        )
        if not data or data.get("prob_yes") is None:
            return None
        try:
            prob = min(max(float(data["prob_yes"]), 0.01), 0.99)
            confidence = float(data.get("confidence") or 0.0)
        except (TypeError, ValueError):
            return None
        rationale = str(data.get("rationale") or "")[:200]
        return Signal(prob_yes=prob, confidence=confidence, rationale=f"llm({confidence:.2f}): {rationale}")
