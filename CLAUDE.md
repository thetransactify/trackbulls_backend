# CLAUDE.md — TrackBulls Backend
> Read this file at the start of EVERY session. It saves tokens by giving Claude full project context instantly.

---

## 🏗️ Project Overview
**TrackBulls** is an AI-powered multi-asset trading & investment platform for Indian markets (NSE equities + MCX commodities). This is the **FastAPI Python backend**.

- **Frontend**: `/var/www/html/trackbulls` (React + Vite)
- **Backend**: `/var/www/html/trackbulls_backend` (FastAPI + Python) ← YOU ARE HERE
- **API Base URL**: `http://localhost:8000/api/v1`
- **API Docs**: `http://localhost:8000/docs` (dev only)

---

## 📁 Project Structure
```
trackbulls_backend/
├── app/
│   ├── main.py                    # FastAPI app entry point
│   ├── api/v1/
│   │   ├── router.py              # Combines all endpoint routers
│   │   └── endpoints/
│   │       ├── auth.py            # POST /auth/login, /auth/refresh, GET /auth/me
│   │       ├── dashboard.py       # GET /dashboard/summary
│   │       ├── portfolio.py       # GET/POST /portfolio, /portfolio/holding
│   │       ├── signals.py         # GET /signals/equity, /signals/mcx, POST /{id}/approve
│   │       ├── orders.py          # POST /orders, GET /orders, POST /{id}/cancel
│   │       ├── risk.py            # GET /risk/status, POST /risk/check, /risk/kill-switch
│   │       ├── instruments.py     # GET /instruments/search, /instruments/screener
│   │       ├── reports.py         # GET /reports/daily, /reports/monthly
│   │       └── app_settings.py    # GET/POST /settings, /settings/broker, /settings/strategies
│   ├── core/
│   │   ├── config.py              # All settings from .env (pydantic-settings)
│   │   ├── security.py            # JWT create/decode, bcrypt password hashing
│   │   └── deps.py                # FastAPI dependencies: get_current_user, require_founder
│   ├── db/
│   │   ├── session.py             # SQLAlchemy engine + SessionLocal + Base + get_db
│   │   └── init_db.py             # create_tables() + seed default admin + strategies
│   ├── models/
│   │   └── models.py              # ALL ORM models (see Database Schema section below)
│   ├── services/
│   │   ├── engines/
│   │   │   ├── equity_engine.py   # score_equity(FundamentalsInput) → ScoreResult (0–100)
│   │   │   └── mcx_engine.py      # score_mcx(MCXInput) → MCXSignalResult (bull/bear score)
│   │   └── broker/                # Zerodha Kite integration (Phase 2)
│   ├── jobs/                      # Celery background tasks (Phase 2)
│   └── utils/                     # Shared helpers
├── scripts/
│   └── setup_db.sh                # One-time PostgreSQL setup
├── tests/                         # pytest tests
├── logs/                          # App logs (auto-created)
├── requirements.txt               # All Python dependencies
├── .env.example                   # Environment variable template
└── .env                           # Your actual env (never commit)
```

---

## 🗄️ Database Schema (all tables in models/models.py)

| Table | Purpose |
|---|---|
| `users` | Login, roles (FOUNDER/ANALYST/TRADER), auth |
| `strategies` | Scoring rule configs per asset type |
| `instruments` | Master list — equities, ETFs, MCX contracts |
| `fundamentals_snapshots` | PE, ROE, EPS, debt, FII/DII history |
| `market_snapshots` | OHLCV + RSI + SMA time series |
| `macro_events` | Budget, weather, geopolitical tags |
| `scores` | 0–100 AI scores with factor breakdown JSON |
| `signals` | BUY/SELL/HOLD signals with confidence, status |
| `holdings` | Current portfolio positions |
| `orders` | Paper/live order lifecycle |
| `reviews` | Periodic review tasks per holding |
| `alerts` | System/risk/signal alerts |
| `audit_logs` | Immutable change log |
| `app_settings` | Key-value config store |

---

## 🔑 Auth Flow
1. `POST /api/v1/auth/login` → `{access_token, refresh_token}`
2. All other requests: `Authorization: Bearer <access_token>`
3. Roles: `FOUNDER` > `TRADER` > `ANALYST`
4. Default seeded user: `admin` / `admin@123` (change in production)

---

## ⚙️ Tech Stack
| Layer | Technology |
|---|---|
| Framework | FastAPI 0.115 |
| Database | PostgreSQL (prod) / SQLite (dev, USE_SQLITE=true) |
| ORM | SQLAlchemy 2.0 |
| Auth | JWT (python-jose) + bcrypt (passlib) |
| Cache/Queue | Redis + Celery |
| AI/ML | pandas, numpy, scikit-learn, pandas-ta |
| Broker | Zerodha Kite Connect SDK |
| Alerts | Telegram Bot + SMTP email |

---

## 🚀 How to Run (Dev)
```bash
cd /var/www/html/trackbulls_backend
cp .env.example .env          # fill in values
pip install -r requirements.txt
python -m app.db.init_db      # creates tables + seeds admin
uvicorn app.main:app --reload --port 8000
```
For SQLite (no PostgreSQL needed): set `USE_SQLITE=true` in `.env`

---

## 📡 All API Endpoints

### Auth
- `POST /api/v1/auth/login` — `{username, password}` → tokens
- `POST /api/v1/auth/refresh` — `{refresh_token}` → new access token
- `GET  /api/v1/auth/me` — current user info
- `POST /api/v1/auth/logout`

### Dashboard
- `GET /api/v1/dashboard/summary` — KPIs, alerts, recent orders

### Portfolio
- `GET  /api/v1/portfolio` — all holdings + allocation
- `POST /api/v1/portfolio/holding` — add holding
- `DEL  /api/v1/portfolio/holding/{id}` — remove holding

### Signals
- `GET  /api/v1/signals/equity` — equity signals (filter: side, status)
- `GET  /api/v1/signals/mcx` — MCX signals
- `GET  /api/v1/signals/{id}` — signal detail
- `POST /api/v1/signals/{id}/approve` — approve signal
- `POST /api/v1/signals/{id}/reject`

### Orders
- `POST /api/v1/orders` — create paper/live order
- `GET  /api/v1/orders` — order blotter (filter: mode, status)
- `GET  /api/v1/orders/{id}` — order detail
- `POST /api/v1/orders/{id}/cancel`

### Risk
- `GET  /api/v1/risk/status` — daily P&L, exposure, kill switch state
- `POST /api/v1/risk/check` — pre-trade validation
- `POST /api/v1/risk/kill-switch` — FOUNDER only — cancel all orders

### Instruments
- `GET /api/v1/instruments/search?q=` — global search
- `GET /api/v1/instruments/screener` — filter by cap, score, PE, ROE
- `GET /api/v1/instruments/{symbol}` — detail + scores + fundamentals
- `POST /api/v1/instruments` — add instrument (FOUNDER only)

### Reports
- `GET /api/v1/reports/daily?report_date=YYYY-MM-DD`
- `GET /api/v1/reports/monthly?year=&month=`

### Settings
- `GET  /api/v1/settings` — all key-value settings
- `POST /api/v1/settings` — upsert setting
- `POST /api/v1/settings/broker` — save Zerodha API keys
- `GET  /api/v1/settings/strategies` — list strategies
- `POST /api/v1/settings/strategies/{id}` — update strategy thresholds

---

## 🧠 Scoring Logic Summary

### Equity (equity_engine.py)
- **Input**: PE, ROE, EPS growth, Sales growth, Profit growth, D/E, FII%, DII%, management score, macro score, cap bucket
- **Output**: Score 0–100, band (STRONG_BUY/BUY/HOLD/WATCH/REJECT), per-factor breakdown JSON
- **Buckets**: LARGE (PE≤60, ROE≥15, D/E≤1.5), MID (PE≤80, ROE≥12, D/E≤2.0), SMALL (PE≤100, ROE≥10, D/E≤2.5)

### MCX (mcx_engine.py)
- **Input**: RSI, SMA20, price, volume trend, supply/demand, contract bias%, geopolitics, budget, weather, days to expiry
- **Output**: bull_score, bear_score, signal (BUY/SELL/NO_TRADE), confidence, target 3%, stop 1.5%
- **Key rule**: >70% contract bias = strong directional signal; RSI <40 = bullish, >65 = bearish

---

## 📋 Development Roadmap
- **Phase 1 (Current)**: Auth, models, all CRUD endpoints, scoring engines ✅
- **Phase 2**: Zerodha Kite live integration, real price feed, Celery jobs, Telegram alerts
- **Phase 3**: Advanced ML confidence model, news sentiment, multi-broker, mobile optimization

---

## ⚠️ Important Rules for Claude
1. **Always check models/models.py** before creating new DB queries — all tables are defined there
2. **Use `get_db` dependency** for all DB sessions — never create sessions manually in endpoints
3. **Use `get_current_user` / `require_founder`** from `core/deps.py` for auth in every endpoint
4. **Paper mode first** — all order creation defaults to PAPER, live requires explicit PAPER_MODE=false
5. **Config from settings** — never hardcode thresholds, use `from app.core.config import settings`
6. **Scoring engines are pure functions** — no DB calls inside equity_engine.py or mcx_engine.py
7. **All new endpoints go in** `app/api/v1/endpoints/` and must be registered in `app/api/v1/router.py`
