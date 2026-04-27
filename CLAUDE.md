# CLAUDE.md — TrackBulls Backend
> Read at the start of every session. Single source of truth as of Module 9.

## Project
| | |
|---|---|
| App | TrackBulls AI Trading Platform (NSE Equities + MCX Commodities) |
| Backend | FastAPI Python — `/var/www/html/trackbulls_backend` |
| Frontend | React + Vite — `/var/www/html/trackbulls` |
| API Base | `http://localhost:8000/api/v1` |
| Docs | `http://localhost:8000/docs` |
| DB | SQLite dev at `trackbulls_dev.db` — `USE_SQLITE=true` in `.env` |
| Login | `admin` / `admin@123` |

## Run Commands
```bash
source venv/bin/activate && uvicorn app.main:app --reload --port 8000
python -m app.db.init_db          # create tables + seed admin
python scripts/seed_instruments.py # seed sample instruments + holding
```

## File Structure — app/
| File | Purpose |
|---|---|
| `main.py` | FastAPI app, CORS, router registration |
| `api/v1/router.py` | Combines all 11 endpoint routers |
| `api/v1/endpoints/auth.py` | JWT login, refresh, logout, /me |
| `api/v1/endpoints/dashboard.py` | Summary KPIs + stats |
| `api/v1/endpoints/portfolio.py` | Holdings CRUD + allocation |
| `api/v1/endpoints/signals.py` | Equity/MCX signal generation, approve/reject |
| `api/v1/endpoints/orders.py` | Paper trading order lifecycle, blotter, P&L |
| `api/v1/endpoints/risk.py` | Risk snapshot, kill switch, alerts |
| `api/v1/endpoints/instruments.py` | Search, screener, fundamentals |
| `api/v1/endpoints/reports.py` | Daily/monthly reports, equity curve, CSV export |
| `api/v1/endpoints/scoring.py` | AI score leaderboard + per-instrument history |
| `api/v1/endpoints/mcx.py` | MCX signals, contracts, macro events |
| `api/v1/endpoints/app_settings.py` | Key-value settings + strategies |
| `core/config.py` | Pydantic settings from `.env` (`DEFAULT_CAPITAL`, etc.) |
| `core/security.py` | JWT encode/decode, bcrypt password hashing |
| `core/deps.py` | `get_current_user`, `require_founder` FastAPI deps |
| `db/session.py` | SQLAlchemy engine + `SessionLocal` + `get_db` |
| `db/init_db.py` | `create_tables()` + seed default admin + strategies |
| `models/models.py` | All 14 ORM models + enums |
| `services/engines/equity_engine.py` | Equity scoring — pure function |
| `services/engines/mcx_engine.py` | MCX bull/bear scoring — pure function |
| `services/engines/signal_engine.py` | Signal generation from scores |
| `services/engines/order_engine.py` | Order validation + UID + P&L — pure |
| `services/engines/risk_engine.py` | Risk snapshot + rule evaluation — DB-aware |
| `services/engines/report_engine.py` | Daily/monthly report + equity curve calcs |

## All API Endpoints (76 total)

**Auth** `/auth`
`POST /login` `POST /refresh` `GET /me` `POST /logout`

**Dashboard** `/dashboard`
`GET /summary` `GET /stats`

**Portfolio** `/portfolio`
`GET /` `GET /allocation` `GET /holding/{id}` `POST /holding` `PUT /holding/{id}` `DELETE /holding/{id}`

**Signals** `/signals`
`GET /equity` `GET /mcx` `GET /stats` `GET /{id}` `POST /generate/equity` `POST /generate/batch` `POST /{id}/approve` `POST /{id}/reject`

**Orders** `/orders`
`POST /` `GET /` `GET /summary` `GET /blotter` `GET /pnl-summary` `GET /open-positions` `GET /by-instrument/{id}` `GET /{id}` `POST /{id}/cancel` `POST /{id}/notes` `POST /bulk-cancel`

**Risk** `/risk`
`GET /status` `GET /exposure` `GET /daily-summary` `GET /alerts` `GET /history` `POST /alerts` `POST /alerts/{id}/acknowledge` `POST /kill-switch` `POST /kill-switch/reset` `POST /check`

**Instruments** `/instruments`
`GET /search` `GET /screener` `GET /sectors` `GET /mcx` `GET /{symbol}` `POST /` `POST /{id}/fundamentals`

**Reports** `/reports`
`GET /daily` `GET /monthly` `GET /equity-curve` `GET /performance` `GET /instruments` `GET /summary-cards` `GET /export/daily` `GET /export/monthly`

**Settings** `/settings`
`GET /` `POST /` `POST /broker` `GET /strategies` `POST /strategies/{id}`

**Scoring** `/scoring`
`GET /leaderboard` `GET /summary` `GET /needs-attention` `GET /equity/{id}/history` `POST /equity/{id}` `POST /equity/batch`

**MCX** `/mcx`
`GET /signals` `GET /dashboard` `GET /macro-events` `GET /session-status` `GET /contracts/{symbol}` `POST /signals/generate/{id}` `POST /signals/generate/batch` `POST /macro-events` `POST /contracts/{symbol}/set-expiry`

## Database Tables (14)
| Table | Key Fields |
|---|---|
| `users` | id, name, email, username, hashed_password, role, is_active |
| `strategies` | id, name, asset_type, mode, status, config_json |
| `instruments` | id, symbol, exchange, asset_type, sector, cap_bucket, expiry |
| `fundamentals_snapshots` | id, instrument_id, as_of_date, pe, roe, eps, debt_equity, fii_pct |
| `market_snapshots` | id, instrument_id, ts, open, high, low, close, volume, rsi, sma_20 |
| `macro_events` | id, type, title, sentiment, effective_from, tags_json |
| `scores` | id, instrument_id, strategy_id, ts, score_value, band, factors_json |
| `signals` | id, instrument_id, strategy_id, ts, side, confidence, status, reasons_json |
| `holdings` | id, instrument_id, quantity, avg_cost, asset_bucket, thesis_status |
| `orders` | id, signal_id, user_id, side, quantity, price, mode, status, filled_qty, filled_price, raw_payload_json |
| `reviews` | id, instrument_id, review_type, due_date, completed_at, outcome |
| `alerts` | id, severity, category, message, related_entity_type, acknowledged_at |
| `audit_logs` | id, actor_user_id, action, entity_type, entity_id, before_json, after_json |
| `app_settings` | id, key, value, updated_by |

## Services & Engines
| Engine | Main Functions |
|---|---|
| `equity_engine.py` | `score_equity(FundamentalsInput) → ScoreResult` — pure, no DB |
| `mcx_engine.py` | `score_mcx(MCXInput) → MCXSignalResult` — pure, no DB |
| `signal_engine.py` | `generate_equity_signal(SignalInput) → SignalOutput` |
| `order_engine.py` | `validate_order()`, `generate_order_uid()`, `calculate_order_pnl()` — pure |
| `risk_engine.py` | `calculate_risk_snapshot(db, capital, max_loss_pct)`, `evaluate_risk_rules(snapshot, settings)` |
| `report_engine.py` | `calculate_daily_report(db, date, capital)`, `calculate_monthly_report(db, y, m, capital)`, `calculate_equity_curve(db, days)`, `calculate_performance_stats(db, capital, period)` |

## Modules Built
| # | What Was Built |
|---|---|
| 1 | FastAPI setup, JWT auth, all DB models, seed scripts |
| 2 | Dashboard connected to real backend — KPIs, portfolio, signals |
| 3 | Portfolio page — holdings CRUD, allocation pie, P&L summary |
| 4 | Equity Trading + Screener + scoring engine (equity_engine) |
| 5 | AI Signals page — equity + MCX signals, approve/reject flow |
| 6 | MCX Trading page — mcx_engine, contracts, macro events, session |
| 7 | Orders page — paper trading blotter, P&L, open positions, create order |
| 8 | Risk Management — kill switch, risk rules, alerts, sidebar status dots |
| 9 | Reports engine + Reports page — daily/monthly/performance/equity curve/exports |

## Coding Rules
1. New endpoints → `app/api/v1/endpoints/` + register in `router.py`
2. Always use `get_db` dependency — never create sessions manually
3. Always use `get_current_user` or `require_founder` on every endpoint
4. Config from `settings` — never hardcode thresholds or URLs
5. `PAPER_MODE=true` by default — live requires explicit override
6. Scoring engines (`equity_engine`, `mcx_engine`) must stay **pure** — no DB calls
7. Use `generate_order_uid()` for all order UIDs
8. Run `validate_order()` before every order creation
9. `instrument_id` on orders stored in `raw_payload_json` — use `_order_instrument_id()` helper to resolve
10. Static paths must be registered **before** `/{id}` paths to avoid FastAPI coercion errors

## What Comes Next
| Module | Plan |
|---|---|
| 10 | Settings page — broker API keys, strategy thresholds, AI config |
| 11 | Reviews & Alerts UI — review scheduler, alert feed |
| 12 | Zerodha Kite live integration — broker service, live orders |
| 13 | Celery background jobs — signal generation, score refresh |
| 14 | Telegram alerts — bot notifications for signals and risk events |
| 15 | PostgreSQL migration — switch from SQLite, connection pooling |
