"""Runtime configuration, loaded from environment / .env (prefix POLYBOT_)."""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="POLYBOT_",
        extra="ignore",
    )

    # Mode: "paper" | "live". Phase 1 stays on paper.
    mode: str = "paper"

    # --- Polymarket APIs ---
    gamma_base_url: str = "https://gamma-api.polymarket.com"
    clob_base_url: str = "https://clob.polymarket.com"
    http_timeout: float = 20.0

    # --- Bankroll & risk (USD) ---
    bankroll_usd: float = 100.0
    max_position_usd: float = 15.0
    max_total_exposure_usd: float = 80.0
    daily_loss_limit_usd: float = 20.0
    min_edge: float = 0.05  # min edge (probability points) to place a bet

    # --- Stage-0 screener thresholds ---
    screen_min_liquidity_usd: float = 1000.0
    screen_min_volume24h_usd: float = 0.0
    screen_max_days_to_resolve: int = 120
    screen_min_hours_to_resolve: float = 6.0
    screen_max_spread: float = 0.08
    screen_price_low: float = 0.05
    screen_price_high: float = 0.95

    # --- LLM keys (unused in the Phase 1 data slice) ---
    grok_api_key: str | None = None
    anthropic_api_key: str | None = None

    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
