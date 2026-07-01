# Polymarket Auto-Betting Bot

Автоматические ставки на [Polymarket](https://polymarket.com) на основе анализа рынка и LLM. Управление — через Telegram-бота. Цель — работать в плюс при небольшом банке.

> ⚠️ Экспериментальный проект. Не является финансовым советом. Ставки на prediction-маркетах — высокий риск потери средств.

## Статус

**Фаза 1 — paper trading MVP** (симуляция без реальных денег). Переход на live только после прохождения gate по калибровке и ROI.

## Идея и экономика

- Банк: небольшой (стартово ~$100), **только торговый капитал**; расходы на LLM API — отдельно, как R&D.
- Приоритет — точность сигналов, а не объём сделок.
- Стоимость LLM не должна съедать банк → двухступенчатая воронка:

```
Все рынки Polymarket
      │  Stage 0: скрин кодом (ликвидность / спред / дата резолва / свежесть) — без LLM
      ▼
  ~50 кандидатов
      │  Stage 1: дешёвый триаж (Claude Haiku)
      ▼
  ~5–10 кандидатов
      │  Stage 2: глубокий разбор (Grok / Claude Sonnet) + prompt caching
      ▼
   Решение
```

## Архитектура (целевая)

- **Data** — Polymarket Gamma API (метаданные) + CLOB API (orderbook/цены, WS).
- **Signal engine** — 4 источника: (1) новости + LLM, (2) микроструктура рынка, (3) соцсети (X/Twitter, Reddit), (4) whale-tracking (on-chain, Polygon).
- **Decision** — оценка вероятности vs рыночная цена → edge → дробный Kelly (с кэпом на позицию).
- **Execution** — paper (симуляция) / live (`py-clob-client`).
- **Telegram** — мониторинг, PnL, старт/стоп, ручной kill-switch.
- **Storage** — лог каждого решения (SQLite → Postgres) для бэктеста и метрик.

## Метрики качества (paper → live gate)

- ≥ ~100–150 закрытых paper-сделок;
- положительный ROI после спреда/слиппеджа;
- Brier score лучше наивного бейзлайна «рыночная цена»;
- адекватная калибровка (calibration curve).

## Live-режим

Полный автомат с жёсткими лимитами: кэп на позицию, лимит суммарной экспозиции, дневной стоп-лосс (kill-switch), минимальный порог edge, минимальная ликвидность рынка.

## Стек

Python 3.11+. Текущие зависимости: `httpx`, `pydantic`, `pydantic-settings`, stdlib `sqlite3`. Планово: `aiogram` (Telegram), `py-clob-client` (live-исполнение), `websockets`. Деплой — Docker на VPS.

## Использование (CLI)

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
cp .env.example .env   # опционально, для правки порогов

.venv/bin/polybot screen --top 20     # Stage-0 скрин рынков
.venv/bin/polybot run --once          # один цикл: resolve → mark → tick
.venv/bin/polybot run --interval 300  # непрерывный paper-цикл (Ctrl-C — стоп)
.venv/bin/polybot report              # метрики: ROI, Brier, калибровка, позиции
# отдельные шаги также доступны: paper-tick / mark / resolve
```

> ⚠️ Стратегия в Phase 1 — **заглушка** (`PlaceholderStrategy`, наивный mean-reversion) для обкатки пайплайна. Её PnL не отражает реальный edge; реальные сигналы — в Phase 2.
