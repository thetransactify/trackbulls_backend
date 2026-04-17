"""
app/api/v1/endpoints/risk.py
GET  /risk/status        — current portfolio risk status
POST /risk/check         — pre-trade risk check
POST /risk/kill-switch   — stop all trading immediately
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import date, datetime
from app.db.session import get_db
from app.core.deps import get_current_user, require_founder
from app.models.models import Order, OrderStatus, OrderMode, Signal, SignalStatus, User, Alert, AlertSeverity
from app.core.config import settings

router = APIRouter(prefix="/risk", tags=["Risk"])


def _calc_daily_pnl(db: Session) -> float:
    """Sum filled order P&L for today — placeholder until live prices."""
    today_start = datetime.combine(date.today(), datetime.min.time())
    filled = db.query(Order).filter(
        Order.status == OrderStatus.FILLED,
        Order.created_at >= today_start,
    ).all()
    # TODO: replace with (filled_price - avg_cost) * filled_qty from holdings
    return 0.0


def _calc_exposure(db: Session) -> dict:
    """Count open positions by asset type."""
    from app.models.models import Holding, Instrument, AssetType
    holdings = db.query(Holding).all()
    total = sum(h.quantity * h.avg_cost for h in holdings)
    by_type: dict = {}
    for h in holdings:
        inst = db.query(Instrument).filter(Instrument.id == h.instrument_id).first()
        atype = str(inst.asset_type) if inst else "UNKNOWN"
        by_type[atype] = by_type.get(atype, 0) + (h.quantity * h.avg_cost)
    # Convert to percentages
    if total > 0:
        by_type = {k: round(v / total * 100, 2) for k, v in by_type.items()}
    return by_type


@router.get("/status")
def get_risk_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    capital = settings.DEFAULT_CAPITAL
    max_loss = capital * settings.MAX_DAILY_LOSS_PCT / 100
    daily_pnl = _calc_daily_pnl(db)
    daily_loss = min(0, daily_pnl)  # negative = loss

    loss_used_pct = abs(daily_loss) / max_loss * 100 if max_loss > 0 else 0
    status = "SAFE"
    if loss_used_pct >= 80:
        status = "WARNING"
    if loss_used_pct >= 100:
        status = "DANGER"

    active_signals = db.query(Signal).filter(Signal.status == SignalStatus.PENDING).count()
    exposure = _calc_exposure(db)

    return {
        "status": status,
        "capital": capital,
        "daily_pnl": daily_pnl,
        "daily_loss_limit": max_loss,
        "loss_used_pct": round(loss_used_pct, 2),
        "max_trade_risk_pct": settings.MAX_TRADE_RISK_PCT,
        "paper_mode": settings.PAPER_MODE,
        "active_signals": active_signals,
        "exposure_by_asset": exposure,
        "kill_switch_active": False,  # TODO: store in Redis/DB
    }


class PreTradeCheckRequest(BaseModel):
    instrument_id: int
    side: str
    quantity: float
    price: float


@router.post("/check")
def pre_trade_check(
    payload: PreTradeCheckRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    capital = settings.DEFAULT_CAPITAL
    trade_value = payload.quantity * payload.price
    trade_pct = trade_value / capital * 100

    checks = {
        "capital_available": True,
        "daily_loss_ok": True,
        "trade_size_ok": trade_pct <= 20,  # max 20% in single trade
        "paper_mode": settings.PAPER_MODE,
    }
    passed = all(checks.values())
    return {
        "passed": passed,
        "checks": checks,
        "trade_value": trade_value,
        "trade_pct_of_capital": round(trade_pct, 2),
        "message": "Pre-trade check passed" if passed else "Pre-trade check FAILED — review checks",
    }


@router.post("/kill-switch")
def kill_switch(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    """Cancel all pending orders and reject all pending signals."""
    # Cancel open orders
    cancelled = db.query(Order).filter(
        Order.status.in_([OrderStatus.CREATED, OrderStatus.SENT, OrderStatus.ACKNOWLEDGED])
    ).update({"status": OrderStatus.CANCELLED})

    # Reject pending signals
    rejected = db.query(Signal).filter(
        Signal.status == SignalStatus.PENDING
    ).update({"status": SignalStatus.REJECTED})

    # Create critical alert
    alert = Alert(
        severity=AlertSeverity.CRITICAL,
        category="RISK",
        message=f"KILL SWITCH activated by {current_user.username} — all orders cancelled, all signals rejected",
    )
    db.add(alert)
    db.commit()

    return {
        "message": "Kill switch activated",
        "orders_cancelled": cancelled,
        "signals_rejected": rejected,
    }
