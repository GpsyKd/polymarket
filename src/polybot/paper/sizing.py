"""Position sizing: fractional Kelly with hard caps.

For a binary market priced at `p` (cost per share, pays $1 on win), a bettor
who estimates the win probability at `q` has Kelly fraction f* = (q - p)/(1 - p)
of bankroll. We use a fraction of that (kelly_mult) and clamp to risk limits.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Bet:
    side: str  # "YES" | "NO"
    price: float  # entry price for the chosen side (0..1)
    prob: float  # model P(chosen side wins)
    edge: float  # prob - price
    size_usd: float
    shares: float


def kelly_fraction(prob: float, price: float) -> float:
    if not (0.0 < price < 1.0):
        return 0.0
    return (prob - price) / (1.0 - price)


def decide_bet(
    prob_yes: float,
    yes_price: float,
    *,
    bankroll: float,
    min_edge: float,
    kelly_mult: float,
    max_position: float,
    min_stake: float,
    remaining_exposure: float,
    fee: float = 0.0,
    market_weight: float = 0.0,
    max_divergence: float | None = None,
) -> Bet | None:
    """Pick the better side (YES/NO) and size it, or return None to skip.

    `fee` is added to the entry price to model crossing the spread (a half-spread
    on entry), so `edge` and sizing are computed against the real fill price.

    `max_divergence` gates on the RAW disagreement with the market: a model that
    differs from a liquid price by a huge margin is far more likely our own error
    (stale/hallucinated info) than a real mispricing, so skip it entirely.
    `market_weight` shrinks the estimate toward the price before sizing, so a
    single over-confident read becomes a moderate bet — a durable edge barely moves.
    """
    if not (0.0 < yes_price < 1.0):
        return None

    if max_divergence is not None and abs(prob_yes - yes_price) > max_divergence:
        return None

    if market_weight > 0.0:
        prob_yes = (1.0 - market_weight) * prob_yes + market_weight * yes_price

    if prob_yes >= yes_price:
        side, base, prob = "YES", yes_price, prob_yes
    else:
        side, base, prob = "NO", 1.0 - yes_price, 1.0 - prob_yes

    price = base + fee
    if not (0.0 < price < 1.0):
        return None

    edge = prob - price
    if edge < min_edge:
        return None

    f = kelly_fraction(prob, price)
    if f <= 0.0:
        return None

    stake = min(kelly_mult * f * bankroll, max_position, remaining_exposure)
    if stake < min_stake:
        return None

    return Bet(side=side, price=price, prob=prob, edge=edge, size_usd=stake, shares=stake / price)
