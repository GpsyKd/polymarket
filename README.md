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

.venv/bin/polybot screen --top 20               # Stage-0 скрин рынков
.venv/bin/polybot run --strategy micro --once   # один цикл микроструктуры
.venv/bin/polybot run --strategy llm --once     # один цикл news+LLM воронки
.venv/bin/polybot run --interval 300            # непрерывный paper-цикл (Ctrl-C — стоп)
.venv/bin/polybot report                        # метрики: ROI, Brier, калибровка, позиции
# отдельные шаги: paper-tick / llm-tick / mark / resolve
```

**Сигналы:**
- `micro` — микроструктура ордербука (imbalance), flow-горизонт с выходом по TP/SL/времени. Flow-входы ограничены узким спредом (`POLYBOT_FLOW_MAX_SPREAD`), иначе спред туда-обратно съедает стоп-лосс.
- `llm` — воронка news+LLM: triage → глубокий разбор (Grok 4.3 + Live Search), держится до резолва. Требует только ключ (`POLYBOT_GROK_API_KEY` или `XAI_API_KEY`/`GROK_API_KEY`); модель по умолчанию — `grok-4.3`. Дефолтный интервал цикла — 30 мин (`POLYBOT_LLM_INTERVAL_SECONDS`).
- `whale` — следование за крупными свежими сделками (окно `POLYBOT_WHALE_MAX_AGE_MINUTES`), держится до резолва.
- `placeholder` — заглушка для обкатки пайплайна (PnL не отражает edge).

Горизонт хранится в каждой позиции: TP/SL/max-hold применяются **только** к flow-позициям; value-ставки (llm/whale) держатся до резолюции — иначе Brier-gate не набрал бы данных.

Реальный edge каждого сигнала проверяется на paper-статистике (`report`: ROI, Brier vs рынок).

## Деплой (Docker, 24/7 на VPS)

```bash
cp .env.example .env      # заполнить ключи (Grok / Telegram)
docker compose up -d      # запустить runner в фоне
docker compose logs -f    # смотреть логи
```

Леджер (SQLite) сохраняется в `./data` (том), переживает рестарты. Стратегию/интервал менять в `command:` в [docker-compose.yml](docker-compose.yml) (`--strategy micro|llm|whale`). Управление на ходу — через Telegram (`/status`, `/pause`, `/resume`).

## Live-исполнение (реальные деньги) ⚠️

По умолчанию всё **paper**. Live включается осознанно и защищён в несколько слоёв:

1. `pip install ".[live]"` — ставит `py-clob-client-v2` (Polymarket CLOB V2).
2. В `.env`: `POLYBOT_MODE=live`, `POLYBOT_POLYGON_PRIVATE_KEY=...` (кошелёк Polygon с USDC); при необходимости `POLYBOT_CLOB_SIGNATURE_TYPE` / `POLYBOT_CLOB_FUNDER` под тип аккаунта (EOA / proxy).
3. Пока **не** задан `POLYBOT_LIVE_CONFIRM=I_UNDERSTAND_LIVE_RISK` — режим **dry-live**: намерение ордера логируется, но ничего не отправляется (безопасная проверка против реального клиента/рынка без трат).
4. Только с `POLYBOT_LIVE_CONFIRM=I_UNDERSTAND_LIVE_RISK` бот реально ставит ордера.

Защиты: хард-кап на размер позиции, лимит суммарной экспозиции, дневной stop-loss (kill-switch), корреляционный лимит, `/pause` в Telegram (переживает рестарт). Покупки — limit-FAK с потолком цены (`POLYBOT_CLOB_SLIPPAGE`), из ответа ордера берётся реальный matched-объём (неисполненный FAK не попадает в леджер). Сайзинг ограничен реальным USDC-балансом. Нет SDK или ключа → автоматический откат на paper. `polybot balance` — показать режим и баланс USDC.

Леджеры paper и live не смешиваются: все расчёты экспозиции и выходы фильтруются по режиму. Для live всё равно заведи отдельный `POLYBOT_DB_PATH`.

> ⚠️ `py-clob-client-v2` новый (CLOB V2, 2026) и этот путь **не тестировался на реальных средствах** при разработке. Первый живой ордер — вручную, крохотной суммой, под наблюдением; проверь `signature_type`/`funder`, USDC-allowances и разбор ответа ордера. Включать live только после успешного прохождения paper-gate.
