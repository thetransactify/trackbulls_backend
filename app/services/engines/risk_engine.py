"""
app/services/engines/risk_engine.py
Real-time portfolio risk calculations.

This service is intentionally DB-aware because risk snapshots aggregate live
orders, holdings, instruments, and signals into a single operational view.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from sqlalchemy.orm import Session, joinedload

from app.models.models import (
    CapBucket,
    Holding,
    Instrument,
    Order,
    OrderStatus,
    Signal,
    SignalSide,
    SignalStatus,
)


@dataclass
class RiskSnapshot:
    capital: float
    daily_pnl: float
    daily_loss: float
    daily_loss_pct: float
    daily_loss_limit: float
    daily_loss_used_pct: float
    status: str
    exposure_by_asset: dict
    exposure_by_bucket: dict
    total_invested: float
    cash_available: float
    active_signals: int
    pending_orders: int
    largest_position: dict
    concentration_risk: str
    kill_switch_active: bool = False


@dataclass
class RiskRule:
    rule_id: str
    name: str
    description: str
    threshold: float
    current_value: float
    status: str
    action: str


def calculate_risk_snapshot(
    db: Session,
    capital: float,
    max_daily_loss_pct: float,
    max_single_position_pct: float = 20.0,
) -> RiskSnapshot:
    today_start = datetime.combine(date.today(), datetime.min.time())
    daily_pnl = _calculate_daily_pnl(db, today_start)
    daily_loss = min(0.0, daily_pnl)

    daily_loss_limit = capital * max_daily_loss_pct / 100.0 if capital > 0 else 0.0
    daily_loss_pct = abs(daily_loss) / capital * 100.0 if capital > 0 else 0.0
    daily_loss_used_pct = (
        abs(daily_loss) / daily_loss_limit * 100.0
        if daily_loss_limit > 0
        else 0.0
    )
    status = _risk_status(daily_loss_used_pct)

    holdings = (
        db.query(Holding)
        .options(joinedload(Holding.instrument))
        .all()
    )
    exposure_by_asset, exposure_by_bucket, total_invested, largest_position = (
        _calculate_exposures(holdings, capital)
    )
    largest_pct = float(largest_position.get("pct_of_capital") or 0.0)
    concentration_risk = _concentration_risk(largest_pct)

    active_signals = (
        db.query(Signal)
        .filter(Signal.status == SignalStatus.PENDING)
        .count()
    )
    pending_orders = (
        db.query(Order)
        .filter(Order.status.in_([OrderStatus.CREATED, OrderStatus.SENT]))
        .count()
    )

    return RiskSnapshot(
        capital=round_float(capital),
        daily_pnl=round_float(daily_pnl),
        daily_loss=round_float(daily_loss),
        daily_loss_pct=round_float(daily_loss_pct),
        daily_loss_limit=round_float(daily_loss_limit),
        daily_loss_used_pct=round_float(daily_loss_used_pct),
        status=status,
        exposure_by_asset=exposure_by_asset,
        exposure_by_bucket=exposure_by_bucket,
        total_invested=round_float(total_invested),
        cash_available=round_float(capital - total_invested),
        active_signals=active_signals,
        pending_orders=pending_orders,
        largest_position=largest_position,
        concentration_risk=concentration_risk,
        kill_switch_active=False,
    )


def evaluate_risk_rules(snapshot: RiskSnapshot, settings: dict) -> list[RiskRule]:
    max_daily_loss_pct = float(settings.get("max_daily_loss_pct", 2.0))
    equity_pct = float(snapshot.exposure_by_asset.get("EQUITY", 0.0) or 0.0)
    largest_pct = float(snapshot.largest_position.get("pct_of_capital", 0.0) or 0.0)
    cash_pct = (
        snapshot.cash_available / snapshot.capital * 100.0
        if snapshot.capital > 0
        else 0.0
    )

    return [
        RiskRule(
            rule_id="daily_loss_limit",
            name="Daily Loss Limit",
            description="Maximum allowed realized loss for the trading day.",
            threshold=round_float(max_daily_loss_pct),
            current_value=round_float(snapshot.daily_loss_pct),
            status=_rule_status(
                breached=snapshot.daily_loss_used_pct >= 100.0,
                warning=snapshot.daily_loss_used_pct >= 80.0,
            ),
            action="Block new orders and activate kill switch if breached.",
        ),
        RiskRule(
            rule_id="single_position_concentration",
            name="Single Position Limit",
            description="Largest holding as a percentage of total capital.",
            threshold=20.0,
            current_value=round_float(largest_pct),
            status=_rule_status(
                breached=largest_pct >= 20.0,
                warning=largest_pct >= 10.0,
            ),
            action="Prevent adding to the largest position if breached.",
        ),
        RiskRule(
            rule_id="equity_overweight",
            name="Equity Allocation",
            description="Maximum allocation to equity holdings.",
            threshold=90.0,
            current_value=round_float(equity_pct),
            status=_rule_status(
                breached=equity_pct > 90.0,
                warning=equity_pct > 80.0,
            ),
            action="Block additional equity exposure if breached.",
        ),
        RiskRule(
            rule_id="cash_buffer",
            name="Minimum Cash Buffer",
            description="Minimum cash reserve as a percentage of capital.",
            threshold=10.0,
            current_value=round_float(cash_pct),
            status=_rule_status(
                breached=cash_pct < 10.0,
                warning=cash_pct < 20.0,
            ),
            action="Block new orders until cash buffer is restored.",
        ),
        RiskRule(
            rule_id="signal_overload",
            name="Active Signals",
            description="Number of pending signals awaiting action.",
            threshold=10.0,
            current_value=round_float(snapshot.active_signals),
            status=_rule_status(
                breached=False,
                warning=snapshot.active_signals > 10,
            ),
            action="Pause signal generation or review stale pending signals.",
        ),
    ]


def get_risk_color(status: str) -> str:
    normalized = _norm(status)
    if normalized in {"SAFE", "OK"}:
        return "green"
    if normalized == "WARNING":
        return "yellow"
    if normalized in {"DANGER", "BREACHED"}:
        return "red"
    return "yellow"


def _calculate_daily_pnl(db: Session, today_start: datetime) -> float:
    filled_orders = (
        db.query(Order)
        .options(joinedload(Order.signal))
        .filter(
            Order.status == OrderStatus.FILLED,
            Order.created_at >= today_start,
        )
        .all()
    )
    if not filled_orders:
        return 0.0

    holding_avg_costs = {
        holding.instrument_id: float(holding.avg_cost or 0.0)
        for holding in db.query(Holding).all()
    }

    daily_pnl = 0.0
    for order in filled_orders:
        instrument_id = _order_instrument_id(order)
        if instrument_id is None:
            continue

        avg_cost = holding_avg_costs.get(instrument_id)
        filled_price = float(order.filled_price or order.price or 0.0)
        filled_qty = float(order.filled_qty or order.quantity or 0.0)
        if avg_cost is None or filled_price <= 0 or filled_qty <= 0:
            continue

        side = _norm(order.side)
        if side == SignalSide.BUY.value:
            daily_pnl += (filled_price - avg_cost) * filled_qty
        elif side == SignalSide.SELL.value:
            daily_pnl += (avg_cost - filled_price) * filled_qty

    return daily_pnl


def _calculate_exposures(
    holdings: list[Holding],
    capital: float,
) -> tuple[dict, dict, float, dict]:
    asset_values = {"EQUITY": 0.0, "MCX": 0.0, "TRADING": 0.0}
    bucket_values = {
        CapBucket.LARGE.value: 0.0,
        CapBucket.MID.value: 0.0,
        CapBucket.SMALL.value: 0.0,
        CapBucket.TRADING.value: 0.0,
    }
    total_invested = 0.0
    largest = {"symbol": None, "value": 0.0, "pct_of_capital": 0.0}

    for holding in holdings:
        value = max(float(holding.quantity or 0.0), 0.0) * float(holding.avg_cost or 0.0)
        if value <= 0:
            continue

        instrument = holding.instrument
        asset_key = _enum_value(getattr(instrument, "asset_type", None), "UNKNOWN")
        bucket_key = _enum_value(
            holding.asset_bucket or getattr(instrument, "cap_bucket", None),
            "TRADING",
        )
        if bucket_key not in bucket_values:
            bucket_values[bucket_key] = 0.0
        if asset_key not in asset_values:
            asset_values[asset_key] = 0.0

        asset_values[asset_key] += value
        bucket_values[bucket_key] += value
        total_invested += value

        if value > largest["value"]:
            largest = {
                "symbol": getattr(instrument, "symbol", None),
                "value": round_float(value),
                "pct_of_capital": round_float(value / capital * 100.0 if capital > 0 else 0.0),
            }

    exposure_by_asset = _pct_map(asset_values, total_invested)
    exposure_by_bucket = _pct_map(bucket_values, total_invested)
    return exposure_by_asset, exposure_by_bucket, total_invested, largest


def _pct_map(values: dict[str, float], denominator: float) -> dict[str, float]:
    if denominator <= 0:
        return {key: 0.0 for key in values}
    return {
        key: round_float(value / denominator * 100.0)
        for key, value in values.items()
    }


def _order_instrument_id(order: Order) -> int | None:
    raw_payload = order.raw_payload_json or {}
    if isinstance(raw_payload, dict) and raw_payload.get("instrument_id") is not None:
        return int(raw_payload["instrument_id"])
    if order.signal and order.signal.instrument_id is not None:
        return int(order.signal.instrument_id)
    return None


def _risk_status(daily_loss_used_pct: float) -> str:
    if daily_loss_used_pct >= 100.0:
        return "BREACHED"
    if daily_loss_used_pct >= 80.0:
        return "DANGER"
    if daily_loss_used_pct >= 50.0:
        return "WARNING"
    return "SAFE"


def _concentration_risk(largest_position_pct: float) -> str:
    if largest_position_pct >= 20.0:
        return "HIGH"
    if largest_position_pct >= 10.0:
        return "MEDIUM"
    return "LOW"


def _rule_status(*, breached: bool, warning: bool) -> str:
    if breached:
        return "BREACHED"
    if warning:
        return "WARNING"
    return "OK"


def _enum_value(value: Any, fallback: str) -> str:
    if value is None:
        return fallback
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)


def _norm(value: Any) -> str:
    return _enum_value(value, "").upper()


def round_float(value: float) -> float:
    return round(float(value or 0.0), 2)
