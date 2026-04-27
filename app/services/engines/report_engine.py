"""
app/services/engines/report_engine.py
Daily and monthly trading report calculations.

DB-aware: queries orders, signals, alerts, holdings, and instruments to
produce structured report data for the /reports endpoints.
"""
from __future__ import annotations

import calendar
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session, joinedload

from app.models.models import (
    Alert,
    Holding,
    Instrument,
    Order,
    OrderStatus,
    Signal,
)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class DailyReportData:
    date: str
    capital: float
    opening_capital: float
    realized_pnl: float
    unrealized_pnl: float = 0.0
    total_pnl: float = 0.0
    pnl_pct: float = 0.0
    total_orders: int = 0
    filled_orders: int = 0
    rejected_orders: int = 0
    cancelled_orders: int = 0
    paper_orders: int = 0
    live_orders: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    breakeven_trades: int = 0
    win_rate_pct: float = 0.0
    avg_win_pnl: float = 0.0
    avg_loss_pnl: float = 0.0
    profit_factor: float = 0.0
    signals_generated: int = 0
    signals_approved: int = 0
    signals_rejected: int = 0
    risk_events: int = 0
    max_single_trade_pnl: float = 0.0
    min_single_trade_pnl: float = 0.0
    by_instrument: list = field(default_factory=list)   # [{symbol, trades, pnl, win_rate}]
    by_asset_type: dict = field(default_factory=dict)   # {EQUITY: pnl, MCX: pnl}


@dataclass
class MonthlyReportData:
    year: int
    month: int
    month_name: str
    capital: float
    total_realized_pnl: float = 0.0
    pnl_pct: float = 0.0
    total_orders: int = 0
    filled_orders: int = 0
    win_trades: int = 0
    loss_trades: int = 0
    win_rate_pct: float = 0.0
    avg_daily_pnl: float = 0.0
    best_day: dict = field(default_factory=dict)        # {date, pnl}
    worst_day: dict = field(default_factory=dict)       # {date, pnl}
    max_drawdown_pct: float = 0.0
    profit_factor: float = 0.0
    avg_win_pnl: float = 0.0
    avg_loss_pnl: float = 0.0
    by_instrument: list = field(default_factory=list)
    by_asset_type: dict = field(default_factory=dict)
    daily_breakdown: list = field(default_factory=list) # [{date, pnl, orders, win_rate}]
    instruments_traded: int = 0


# ── Private helpers ───────────────────────────────────────────────────────────

def _day_range(report_date: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(report_date, datetime.min.time()),
        datetime.combine(report_date, datetime.max.time()),
    )


def _order_instrument_id(order: Order) -> Optional[int]:
    """Resolve instrument_id from raw_payload_json or linked signal."""
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


def _rf(value: float) -> float:
    return round(float(value or 0.0), 2)


def _compute_pnl(order: Order, avg_cost: float) -> float:
    """
    BUY:  (filled_price - avg_cost) * filled_qty   — profitable when price rises
    SELL: (avg_cost - filled_price) * filled_qty   — profitable when price falls (short / close)
    """
    side = _norm(order.side)
    filled_price = float(order.filled_price or order.price or 0.0)
    filled_qty = float(order.filled_qty or order.quantity or 0.0)
    if filled_price <= 0 or filled_qty <= 0:
        return 0.0
    if side == "BUY":
        return (filled_price - avg_cost) * filled_qty
    if side == "SELL":
        return (avg_cost - filled_price) * filled_qty
    return 0.0


def _build_instrument_breakdown(pnl_map: dict[int, dict]) -> list[dict]:
    rows = [
        {
            "symbol": v["symbol"],
            "trades": v["trades"],
            "pnl": _rf(v["pnl"]),
            "win_rate": _rf(v["wins"] / v["trades"] * 100) if v["trades"] > 0 else 0.0,
        }
        for v in pnl_map.values()
    ]
    rows.sort(key=lambda x: x["pnl"], reverse=True)
    return rows


# ── Public API ────────────────────────────────────────────────────────────────

def calculate_daily_report(
    db: Session,
    report_date: date,
    capital: float,
) -> DailyReportData:
    """
    Compute all trading metrics for a single calendar day.
    PnL is realized only — based on filled orders vs. holding avg_cost.
    """
    day_start, day_end = _day_range(report_date)

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

    filled = [o for o in orders if _norm(o.status) == "FILLED"]

    trade_pnls: list[float] = []
    pnl_by_instrument: dict[int, dict] = {}
    asset_pnl: dict[str, float] = {}

    for order in filled:
        instr_id = _order_instrument_id(order)
        if instr_id is None:
            continue
        avg_cost = holding_map.get(instr_id, 0.0)
        if avg_cost <= 0:
            continue

        pnl = _compute_pnl(order, avg_cost)
        trade_pnls.append(pnl)

        instr = instruments.get(instr_id)
        symbol = instr.symbol if instr else str(instr_id)
        asset_type = _norm(getattr(instr, "asset_type", None)) or "UNKNOWN"

        bucket = pnl_by_instrument.setdefault(
            instr_id, {"symbol": symbol, "trades": 0, "pnl": 0.0, "wins": 0}
        )
        bucket["trades"] += 1
        bucket["pnl"] += pnl
        if pnl > 0:
            bucket["wins"] += 1

        asset_pnl[asset_type] = asset_pnl.get(asset_type, 0.0) + pnl

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    breakevens = [p for p in trade_pnls if p == 0.0]
    total_closed = len(wins) + len(losses) + len(breakevens)

    total_win_sum = sum(wins)
    total_loss_abs = abs(sum(losses))
    realized_pnl = _rf(sum(trade_pnls))
    opening_capital = capital - realized_pnl

    signals_today = (
        db.query(Signal)
        .filter(Signal.ts >= day_start, Signal.ts <= day_end)
        .all()
    )
    risk_events = (
        db.query(Alert)
        .filter(
            Alert.category == "RISK",
            Alert.created_at >= day_start,
            Alert.created_at <= day_end,
        )
        .count()
    )

    return DailyReportData(
        date=report_date.isoformat(),
        capital=_rf(capital),
        opening_capital=_rf(opening_capital),
        realized_pnl=realized_pnl,
        unrealized_pnl=0.0,
        total_pnl=realized_pnl,
        pnl_pct=_rf(realized_pnl / opening_capital * 100) if opening_capital > 0 else 0.0,
        total_orders=len(orders),
        filled_orders=len(filled),
        rejected_orders=sum(1 for o in orders if _norm(o.status) == "REJECTED"),
        cancelled_orders=sum(1 for o in orders if _norm(o.status) == "CANCELLED"),
        paper_orders=sum(1 for o in orders if _norm(o.mode) == "PAPER"),
        live_orders=sum(1 for o in orders if _norm(o.mode) == "LIVE"),
        win_trades=len(wins),
        loss_trades=len(losses),
        breakeven_trades=len(breakevens),
        win_rate_pct=_rf(len(wins) / total_closed * 100) if total_closed > 0 else 0.0,
        avg_win_pnl=_rf(total_win_sum / len(wins)) if wins else 0.0,
        avg_loss_pnl=_rf(sum(losses) / len(losses)) if losses else 0.0,
        profit_factor=_rf(total_win_sum / total_loss_abs) if total_loss_abs > 0 else 0.0,
        signals_generated=len(signals_today),
        signals_approved=sum(1 for s in signals_today if _norm(s.status) == "APPROVED"),
        signals_rejected=sum(1 for s in signals_today if _norm(s.status) == "REJECTED"),
        risk_events=risk_events,
        max_single_trade_pnl=_rf(max(trade_pnls)) if trade_pnls else 0.0,
        min_single_trade_pnl=_rf(min(trade_pnls)) if trade_pnls else 0.0,
        by_instrument=_build_instrument_breakdown(pnl_by_instrument),
        by_asset_type={k: _rf(v) for k, v in asset_pnl.items()},
    )


def calculate_monthly_report(
    db: Session,
    year: int,
    month: int,
    capital: float,
) -> MonthlyReportData:
    """
    Aggregate daily reports across every calendar day in the given month.
    Future dates are included in daily_breakdown as zero-pnl placeholders.
    """
    days_in_month = calendar.monthrange(year, month)[1]
    today = date.today()

    # Load reference data once — avoids N×2 extra queries inside the day loop
    holding_map: dict[int, float] = {
        h.instrument_id: float(h.avg_cost or 0.0)
        for h in db.query(Holding).all()
    }
    instruments: dict[int, Instrument] = {
        i.id: i for i in db.query(Instrument).all()
    }

    # Fetch all orders for the whole month in a single query
    month_start = datetime.combine(date(year, month, 1), datetime.min.time())
    month_end = datetime.combine(date(year, month, days_in_month), datetime.max.time())
    all_month_orders = (
        db.query(Order)
        .options(joinedload(Order.signal))
        .filter(Order.created_at >= month_start, Order.created_at <= month_end)
        .all()
    )

    # Bucket orders by calendar day for O(1) per-day lookup
    orders_by_day: dict[int, list[Order]] = {}
    for order in all_month_orders:
        if order.created_at:
            day_key = order.created_at.day
            orders_by_day.setdefault(day_key, []).append(order)

    # Aggregate state
    daily_breakdown = []
    total_realized_pnl = 0.0
    total_orders = 0
    total_filled = 0
    all_wins: list[float] = []
    all_losses: list[float] = []
    pnl_by_instrument: dict[int, dict] = {}
    asset_pnl: dict[str, float] = {}
    instruments_set: set[int] = set()

    best_day: dict = {"date": "", "pnl": float("-inf")}
    worst_day: dict = {"date": "", "pnl": float("inf")}
    running_pnl = 0.0
    peak_pnl = 0.0
    max_drawdown_pct = 0.0

    for day_num in range(1, days_in_month + 1):
        report_date = date(year, month, day_num)

        if report_date > today:
            daily_breakdown.append({
                "date": report_date.isoformat(),
                "pnl": 0.0,
                "orders": 0,
                "win_rate": 0.0,
            })
            continue

        day_orders = orders_by_day.get(day_num, [])
        filled = [o for o in day_orders if _norm(o.status) == "FILLED"]

        day_pnl = 0.0
        day_wins = 0
        day_trades = 0

        for order in filled:
            instr_id = _order_instrument_id(order)
            if instr_id is None:
                continue
            avg_cost = holding_map.get(instr_id, 0.0)
            if avg_cost <= 0:
                continue

            pnl = _compute_pnl(order, avg_cost)
            day_pnl += pnl
            day_trades += 1
            instruments_set.add(instr_id)

            if pnl > 0:
                day_wins += 1
                all_wins.append(pnl)
            elif pnl < 0:
                all_losses.append(pnl)

            instr = instruments.get(instr_id)
            symbol = instr.symbol if instr else str(instr_id)
            asset_type = _norm(getattr(instr, "asset_type", None)) or "UNKNOWN"

            bucket = pnl_by_instrument.setdefault(
                instr_id, {"symbol": symbol, "trades": 0, "pnl": 0.0, "wins": 0}
            )
            bucket["trades"] += 1
            bucket["pnl"] += pnl
            if pnl > 0:
                bucket["wins"] += 1

            asset_pnl[asset_type] = asset_pnl.get(asset_type, 0.0) + pnl

        total_realized_pnl += day_pnl
        total_orders += len(day_orders)
        total_filled += len(filled)

        day_win_rate = _rf(day_wins / day_trades * 100) if day_trades > 0 else 0.0
        daily_breakdown.append({
            "date": report_date.isoformat(),
            "pnl": _rf(day_pnl),
            "orders": len(day_orders),
            "win_rate": day_win_rate,
        })

        if day_pnl > best_day["pnl"]:
            best_day = {"date": report_date.isoformat(), "pnl": _rf(day_pnl)}
        if day_pnl < worst_day["pnl"]:
            worst_day = {"date": report_date.isoformat(), "pnl": _rf(day_pnl)}

        # Peak-to-trough drawdown
        running_pnl += day_pnl
        if running_pnl > peak_pnl:
            peak_pnl = running_pnl
        if capital > 0:
            drawdown_pct = (peak_pnl - running_pnl) / capital * 100
            if drawdown_pct > max_drawdown_pct:
                max_drawdown_pct = drawdown_pct

    # Derived aggregates
    total_closed = len(all_wins) + len(all_losses)
    total_win_sum = sum(all_wins)
    total_loss_abs = abs(sum(all_losses))
    trading_days = sum(1 for d in daily_breakdown if d["orders"] > 0)

    return MonthlyReportData(
        year=year,
        month=month,
        month_name=calendar.month_name[month],
        capital=_rf(capital),
        total_realized_pnl=_rf(total_realized_pnl),
        pnl_pct=_rf(total_realized_pnl / capital * 100) if capital > 0 else 0.0,
        total_orders=total_orders,
        filled_orders=total_filled,
        win_trades=len(all_wins),
        loss_trades=len(all_losses),
        win_rate_pct=_rf(len(all_wins) / total_closed * 100) if total_closed > 0 else 0.0,
        avg_daily_pnl=_rf(total_realized_pnl / trading_days) if trading_days > 0 else 0.0,
        best_day=best_day if best_day["date"] else {"date": "", "pnl": 0.0},
        worst_day=worst_day if worst_day["date"] else {"date": "", "pnl": 0.0},
        max_drawdown_pct=_rf(max_drawdown_pct),
        profit_factor=_rf(total_win_sum / total_loss_abs) if total_loss_abs > 0 else 0.0,
        avg_win_pnl=_rf(total_win_sum / len(all_wins)) if all_wins else 0.0,
        avg_loss_pnl=_rf(sum(all_losses) / len(all_losses)) if all_losses else 0.0,
        by_instrument=_build_instrument_breakdown(pnl_by_instrument),
        by_asset_type={k: _rf(v) for k, v in asset_pnl.items()},
        daily_breakdown=daily_breakdown,
        instruments_traded=len(instruments_set),
    )


def calculate_equity_curve(
    db: Session,
    days: int = 30,
) -> list[dict]:
    """
    Returns [{date, daily_pnl, cumulative_pnl}] for the last N calendar days.
    Used to render the equity curve chart on the frontend.
    """
    today = date.today()
    start_date = today - timedelta(days=days - 1)

    holding_map: dict[int, float] = {
        h.instrument_id: float(h.avg_cost or 0.0)
        for h in db.query(Holding).all()
    }

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(today, datetime.max.time())

    filled_orders = (
        db.query(Order)
        .options(joinedload(Order.signal))
        .filter(
            Order.status == OrderStatus.FILLED,
            Order.created_at >= start_dt,
            Order.created_at <= end_dt,
        )
        .all()
    )

    # Group by date string for fast per-day aggregation
    pnl_by_date: dict[str, float] = {}
    for order in filled_orders:
        if not order.created_at:
            continue
        instr_id = _order_instrument_id(order)
        if instr_id is None:
            continue
        avg_cost = holding_map.get(instr_id, 0.0)
        if avg_cost <= 0:
            continue
        day_key = order.created_at.date().isoformat()
        pnl_by_date[day_key] = pnl_by_date.get(day_key, 0.0) + _compute_pnl(order, avg_cost)

    result = []
    cumulative_pnl = 0.0
    for i in range(days):
        d = start_date + timedelta(days=i)
        key = d.isoformat()
        daily_pnl = pnl_by_date.get(key, 0.0)
        cumulative_pnl += daily_pnl
        result.append({
            "date": key,
            "daily_pnl": _rf(daily_pnl),
            "cumulative_pnl": _rf(cumulative_pnl),
        })

    return result


def calculate_performance_stats(
    db: Session,
    capital: float,
    period: str = "all",
) -> dict:
    """
    Comprehensive performance stats for a given period (today/week/month/all).

    Simplified Sharpe ratio = mean(per-trade returns) / std(per-trade returns) × √252.
    Uses per-trade returns (not daily), which gives a reasonable approximation
    when the number of daily data points is small.
    """
    today = date.today()
    if period == "today":
        start_date = today
    elif period == "week":
        start_date = today - timedelta(days=7)
    elif period == "month":
        start_date = today.replace(day=1)
    else:
        start_date = date(2000, 1, 1)

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(today, datetime.max.time())

    orders = (
        db.query(Order)
        .options(joinedload(Order.signal))
        .filter(
            Order.status == OrderStatus.FILLED,
            Order.created_at >= start_dt,
            Order.created_at <= end_dt,
        )
        .all()
    )

    holding_map: dict[int, float] = {
        h.instrument_id: float(h.avg_cost or 0.0)
        for h in db.query(Holding).all()
    }
    instruments: dict[int, Instrument] = {
        i.id: i for i in db.query(Instrument).all()
    }

    wins: list[float] = []
    losses: list[float] = []
    all_pnls: list[float] = []
    pnl_by_instrument: dict[int, dict] = {}
    hold_times: list[float] = []

    for order in orders:
        instr_id = _order_instrument_id(order)
        if instr_id is None:
            continue
        avg_cost = holding_map.get(instr_id, 0.0)
        if avg_cost <= 0:
            continue

        pnl = _compute_pnl(order, avg_cost)
        all_pnls.append(pnl)

        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(pnl)

        if order.created_at and order.updated_at:
            delta_hrs = (order.updated_at - order.created_at).total_seconds() / 3600.0
            if delta_hrs >= 0:
                hold_times.append(delta_hrs)

        instr = instruments.get(instr_id)
        symbol = instr.symbol if instr else str(instr_id)
        bucket = pnl_by_instrument.setdefault(
            instr_id, {"symbol": symbol, "pnl": 0.0, "trades": 0}
        )
        bucket["pnl"] += pnl
        bucket["trades"] += 1

    total_trades = len(all_pnls)
    total_closed = len(wins) + len(losses)
    total_win_sum = sum(wins)
    total_loss_abs = abs(sum(losses))
    total_pnl = _rf(sum(all_pnls))

    # Simplified Sharpe using per-trade return % vs. capital
    sharpe_ratio = 0.0
    if len(all_pnls) > 1 and capital > 0:
        trade_returns = [p / capital * 100 for p in all_pnls]
        mean_r = sum(trade_returns) / len(trade_returns)
        variance = sum((r - mean_r) ** 2 for r in trade_returns) / len(trade_returns)
        std_r = math.sqrt(variance)
        if std_r > 0:
            sharpe_ratio = _rf(mean_r / std_r * math.sqrt(252))

    best_instrument = None
    worst_instrument = None
    if pnl_by_instrument:
        ranked = sorted(pnl_by_instrument.values(), key=lambda x: x["pnl"], reverse=True)
        best_instrument = {"symbol": ranked[0]["symbol"], "pnl": _rf(ranked[0]["pnl"])}
        worst_instrument = {"symbol": ranked[-1]["symbol"], "pnl": _rf(ranked[-1]["pnl"])}

    return {
        "total_trades": total_trades,
        "win_rate": _rf(len(wins) / total_closed * 100) if total_closed > 0 else 0.0,
        "profit_factor": _rf(total_win_sum / total_loss_abs) if total_loss_abs > 0 else 0.0,
        "avg_win": _rf(total_win_sum / len(wins)) if wins else 0.0,
        "avg_loss": _rf(sum(losses) / len(losses)) if losses else 0.0,
        "max_win": _rf(max(wins)) if wins else 0.0,
        "max_loss": _rf(min(losses)) if losses else 0.0,
        "total_pnl": total_pnl,
        "pnl_pct": _rf(total_pnl / capital * 100) if capital > 0 else 0.0,
        "sharpe_ratio": sharpe_ratio,
        "best_instrument": best_instrument,
        "worst_instrument": worst_instrument,
        "avg_hold_time_hours": _rf(sum(hold_times) / len(hold_times)) if hold_times else 0.0,
    }
