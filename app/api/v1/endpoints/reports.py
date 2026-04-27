"""
app/api/v1/endpoints/reports.py
Full reports engine — daily, monthly, equity curve, performance,
per-instrument breakdown, CSV/JSON exports, summary cards.
"""
from __future__ import annotations

import csv
import io
from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.deps import get_current_user
from app.db.session import get_db
from app.models.models import (
    Holding,
    Instrument,
    Order,
    OrderStatus,
    User,
)
from app.services.engines.report_engine import (
    calculate_daily_report,
    calculate_equity_curve,
    calculate_monthly_report,
    calculate_performance_stats,
)

router = APIRouter(prefix="/reports", tags=["Reports"])

# ── Helpers ───────────────────────────────────────────────────────────────────

def _capital() -> float:
    return float(settings.DEFAULT_CAPITAL)


def _parse_date(date_str: Optional[str]) -> date:
    if date_str:
        try:
            return datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format — use YYYY-MM-DD")
    return date.today()


def _order_instrument_id(order: Order) -> Optional[int]:
    raw = order.raw_payload_json or {}
    if isinstance(raw, dict) and raw.get("instrument_id") is not None:
        return int(raw["instrument_id"])
    if order.signal and order.signal.instrument_id is not None:
        return int(order.signal.instrument_id)
    return None


def _norm(value) -> str:
    if value is None:
        return ""
    if hasattr(value, "value"):
        return str(value.value)
    return str(value).upper()


def _rf(v: float) -> float:
    return round(float(v or 0.0), 2)


# ── 1. Daily report ───────────────────────────────────────────────────────────

@router.get("/daily")
def daily_report(
    report_date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full daily performance report with comparison to previous trading day."""
    day = _parse_date(report_date)
    capital = _capital()

    today_data = calculate_daily_report(db, day, capital)
    result = asdict(today_data)

    # Compare to previous calendar day
    prev_day = day - timedelta(days=1)
    try:
        prev_data = calculate_daily_report(db, prev_day, capital)
        result["comparison"] = {
            "pnl_vs_yesterday": _rf(today_data.realized_pnl - prev_data.realized_pnl),
            "win_rate_vs_yesterday": _rf(today_data.win_rate_pct - prev_data.win_rate_pct),
            "orders_vs_yesterday": today_data.total_orders - prev_data.total_orders,
        }
    except Exception:
        result["comparison"] = {
            "pnl_vs_yesterday": 0.0,
            "win_rate_vs_yesterday": 0.0,
            "orders_vs_yesterday": 0,
        }

    return result


# ── 2. Monthly report ─────────────────────────────────────────────────────────

@router.get("/monthly")
def monthly_report(
    year: Optional[int] = Query(None, description="4-digit year, defaults to current"),
    month: Optional[int] = Query(None, ge=1, le=12, description="1–12, defaults to current"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Full monthly performance report with daily breakdown and drawdown."""
    today = date.today()
    y = year or today.year
    m = month or today.month
    capital = _capital()

    data = calculate_monthly_report(db, y, m, capital)
    return asdict(data)


# ── 3. Equity curve ───────────────────────────────────────────────────────────

@router.get("/equity-curve")
def equity_curve(
    days: int = Query(30, ge=1, le=365, description="Number of calendar days to include"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Cumulative P&L curve — one data point per calendar day."""
    curve = calculate_equity_curve(db, days)
    return {"curve": curve, "period_days": days}


# ── 4. Performance stats ──────────────────────────────────────────────────────

@router.get("/performance")
def performance_stats(
    period: str = Query("all", description="today | week | month | all"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Comprehensive performance statistics for a given period."""
    if period not in ("today", "week", "month", "all"):
        raise HTTPException(status_code=400, detail="period must be today, week, month, or all")
    return calculate_performance_stats(db, _capital(), period)


# ── 5. Per-instrument breakdown ───────────────────────────────────────────────

@router.get("/instruments")
def instrument_breakdown(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """All-time performance breakdown sorted by total P&L descending."""
    holding_map: dict[int, float] = {
        h.instrument_id: float(h.avg_cost or 0.0)
        for h in db.query(Holding).all()
    }
    instruments: dict[int, Instrument] = {
        i.id: i for i in db.query(Instrument).all()
    }

    filled_orders = (
        db.query(Order)
        .options(joinedload(Order.signal))
        .filter(Order.status == OrderStatus.FILLED)
        .all()
    )

    # Aggregate per instrument
    agg: dict[int, dict] = {}
    for order in filled_orders:
        instr_id = _order_instrument_id(order)
        if instr_id is None:
            continue
        avg_cost = holding_map.get(instr_id, 0.0)
        if avg_cost <= 0:
            continue

        side = _norm(order.side)
        filled_price = float(order.filled_price or order.price or 0.0)
        filled_qty = float(order.filled_qty or order.quantity or 0.0)
        if filled_price <= 0 or filled_qty <= 0:
            continue

        if side == "BUY":
            pnl = (filled_price - avg_cost) * filled_qty
        elif side == "SELL":
            pnl = (avg_cost - filled_price) * filled_qty
        else:
            continue

        instr = instruments.get(instr_id)
        if instr_id not in agg:
            agg[instr_id] = {
                "instrument_id": instr_id,
                "symbol": instr.symbol if instr else str(instr_id),
                "sector": getattr(instr, "sector", None) or "",
                "cap_bucket": _norm(getattr(instr, "cap_bucket", None)),
                "total_trades": 0,
                "win_trades": 0,
                "loss_trades": 0,
                "total_pnl": 0.0,
                "trade_pnls": [],
            }
        row = agg[instr_id]
        row["total_trades"] += 1
        row["total_pnl"] += pnl
        row["trade_pnls"].append(pnl)
        if pnl > 0:
            row["win_trades"] += 1
        elif pnl < 0:
            row["loss_trades"] += 1

    # Build response — drop internal trade_pnls list
    rows = []
    for row in agg.values():
        pnls = row.pop("trade_pnls")
        n = row["total_trades"]
        rows.append({
            **row,
            "total_pnl": _rf(row["total_pnl"]),
            "win_rate_pct": _rf(row["win_trades"] / n * 100) if n > 0 else 0.0,
            "avg_pnl_per_trade": _rf(row["total_pnl"] / n) if n > 0 else 0.0,
            "best_trade_pnl": _rf(max(pnls)) if pnls else 0.0,
            "worst_trade_pnl": _rf(min(pnls)) if pnls else 0.0,
        })

    rows.sort(key=lambda x: x["total_pnl"], reverse=True)
    return rows


# ── 6. Export daily (CSV / JSON) ──────────────────────────────────────────────

@router.get("/export/daily")
def export_daily(
    report_date: Optional[str] = Query(None, description="YYYY-MM-DD, defaults to today"),
    format: str = Query("csv", description="csv or json"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export daily orders as CSV or JSON. CSV columns: Date, Symbol, Side, Qty, Price, PnL, Status."""
    day = _parse_date(report_date)

    if format not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be csv or json")

    day_start = datetime.combine(day, datetime.min.time())
    day_end = datetime.combine(day, datetime.max.time())

    orders = (
        db.query(Order)
        .options(joinedload(Order.signal))
        .filter(Order.created_at >= day_start, Order.created_at <= day_end)
        .all()
    )

    holding_map: dict[int, float] = {
        h.instrument_id: float(h.avg_cost or 0.0)
        for h in db.query(Holding).all()
    }
    instruments: dict[int, Instrument] = {
        i.id: i for i in db.query(Instrument).all()
    }

    rows = _orders_to_export_rows(orders, holding_map, instruments, day)

    if format == "json":
        return rows

    # CSV
    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["Date", "Symbol", "Side", "Qty", "Price", "PnL", "Status"],
    )
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)
    filename = f"trackbulls_daily_{day.isoformat()}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── 7. Export monthly (CSV / JSON) ────────────────────────────────────────────

@router.get("/export/monthly")
def export_monthly(
    year: Optional[int] = Query(None),
    month: Optional[int] = Query(None, ge=1, le=12),
    format: str = Query("csv", description="csv or json"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Export all orders for a month as CSV or JSON — one row per order."""
    import calendar

    if format not in ("csv", "json"):
        raise HTTPException(status_code=400, detail="format must be csv or json")

    today = date.today()
    y = year or today.year
    m = month or today.month
    days_in_month = calendar.monthrange(y, m)[1]

    month_start = datetime.combine(date(y, m, 1), datetime.min.time())
    month_end = datetime.combine(date(y, m, days_in_month), datetime.max.time())

    orders = (
        db.query(Order)
        .options(joinedload(Order.signal))
        .filter(Order.created_at >= month_start, Order.created_at <= month_end)
        .all()
    )

    holding_map: dict[int, float] = {
        h.instrument_id: float(h.avg_cost or 0.0)
        for h in db.query(Holding).all()
    }
    instruments: dict[int, Instrument] = {
        i.id: i for i in db.query(Instrument).all()
    }

    rows = _orders_to_export_rows(orders, holding_map, instruments)

    if format == "json":
        return rows

    output = io.StringIO()
    writer = csv.DictWriter(
        output,
        fieldnames=["Date", "Symbol", "Side", "Qty", "Price", "PnL", "Status"],
    )
    writer.writeheader()
    writer.writerows(rows)
    output.seek(0)
    filename = f"trackbulls_monthly_{y}_{m:02d}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ── 8. Summary cards ──────────────────────────────────────────────────────────

@router.get("/summary-cards")
def summary_cards(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Quick aggregated stats for dashboard / reports header.
    Returns today, week, month, and all-time buckets.
    """
    capital = _capital()

    def _bucket(period: str) -> dict:
        stats = calculate_performance_stats(db, capital, period)
        return {
            "pnl": stats["total_pnl"],
            "orders": stats["total_trades"],
            "win_rate": stats["win_rate"],
        }

    today_stats = _bucket("today")
    week_stats = _bucket("week")
    month_stats = _bucket("month")
    all_stats = calculate_performance_stats(db, capital, "all")

    # Best and worst day (last 90 days scan — lightweight)
    best_day = {"date": "", "pnl": 0.0}
    worst_day = {"date": "", "pnl": 0.0}
    best_pnl = float("-inf")
    worst_pnl = float("inf")
    today = date.today()

    holding_map: dict[int, float] = {
        h.instrument_id: float(h.avg_cost or 0.0)
        for h in db.query(Holding).all()
    }

    scan_start = datetime.combine(today - timedelta(days=90), datetime.min.time())
    scan_end = datetime.combine(today, datetime.max.time())
    filled = (
        db.query(Order)
        .options(joinedload(Order.signal))
        .filter(Order.status == OrderStatus.FILLED, Order.created_at >= scan_start, Order.created_at <= scan_end)
        .all()
    )

    pnl_by_date: dict[str, float] = {}
    for order in filled:
        if not order.created_at:
            continue
        instr_id = _order_instrument_id(order)
        if instr_id is None:
            continue
        avg_cost = holding_map.get(instr_id, 0.0)
        if avg_cost <= 0:
            continue

        side = _norm(order.side)
        fp = float(order.filled_price or order.price or 0.0)
        fq = float(order.filled_qty or order.quantity or 0.0)
        if fp <= 0 or fq <= 0:
            continue

        pnl = (fp - avg_cost) * fq if side == "BUY" else (avg_cost - fp) * fq
        key = order.created_at.date().isoformat()
        pnl_by_date[key] = pnl_by_date.get(key, 0.0) + pnl

    for d_str, d_pnl in pnl_by_date.items():
        if d_pnl > best_pnl:
            best_pnl = d_pnl
            best_day = {"date": d_str, "pnl": _rf(d_pnl)}
        if d_pnl < worst_pnl:
            worst_pnl = d_pnl
            worst_day = {"date": d_str, "pnl": _rf(d_pnl)}

    return {
        "today": today_stats,
        "week": week_stats,
        "month": month_stats,
        "all_time": {
            "pnl": all_stats["total_pnl"],
            "orders": all_stats["total_trades"],
            "win_rate": all_stats["win_rate"],
            "best_day": best_day,
            "worst_day": worst_day,
        },
    }


# ── Private helpers ───────────────────────────────────────────────────────────

def _orders_to_export_rows(
    orders: list[Order],
    holding_map: dict[int, float],
    instruments: dict[int, Instrument],
    filter_date: Optional[date] = None,
) -> list[dict]:
    rows = []
    for order in orders:
        instr_id = _order_instrument_id(order)
        instr = instruments.get(instr_id) if instr_id else None
        symbol = instr.symbol if instr else ""

        avg_cost = holding_map.get(instr_id, 0.0) if instr_id else 0.0
        side = _norm(order.side)
        fp = float(order.filled_price or order.price or 0.0)
        fq = float(order.filled_qty or order.quantity or 0.0)

        pnl = 0.0
        if avg_cost > 0 and fp > 0 and fq > 0 and _norm(order.status) == "FILLED":
            pnl = (fp - avg_cost) * fq if side == "BUY" else (avg_cost - fp) * fq

        order_date = order.created_at.date().isoformat() if order.created_at else ""
        rows.append({
            "Date": order_date,
            "Symbol": symbol,
            "Side": side,
            "Qty": fq,
            "Price": fp,
            "PnL": _rf(pnl),
            "Status": _norm(order.status),
        })
    return rows
