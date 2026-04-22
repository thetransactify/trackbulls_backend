"""
app/api/v1/endpoints/dashboard.py
GET /dashboard/summary — command center KPIs
GET /dashboard/stats   — instrument/signal/order counts
"""
from datetime import date, datetime
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.db.session import get_db
from app.core.deps import get_current_user
from app.core.config import settings
from app.models.models import (
    Holding, Order, Signal, Alert, Instrument, Strategy,
    OrderStatus, SignalStatus, AlertSeverity, User,
)

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/summary")
def get_dashboard_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Total holdings value (placeholder — real price feed in Phase 2)
    holdings = db.query(Holding).all()
    invested = sum(h.quantity * h.avg_cost for h in holdings)

    # Active signals
    active_signals = db.query(Signal).filter(
        Signal.status == SignalStatus.PENDING
    ).count()

    # Today's orders
    today_start = datetime.combine(date.today(), datetime.min.time())
    todays_orders = db.query(Order).filter(Order.created_at >= today_start).count()

    # Unacknowledged critical alerts
    critical_alerts = db.query(Alert).filter(
        Alert.severity == AlertSeverity.CRITICAL,
        Alert.acknowledged_at == None
    ).count()

    # Recent alerts
    recent_alerts = db.query(Alert).order_by(Alert.created_at.desc()).limit(5).all()

    # Recent orders
    recent_orders = db.query(Order).order_by(Order.created_at.desc()).limit(5).all()

    # Active strategies
    active_strategies = db.query(Strategy).filter(Strategy.status == "ACTIVE").all()

    # Portfolio allocation by asset bucket (% of total invested)
    allocation: dict = {}
    if holdings and invested > 0:
        bucket_totals: dict = {}
        for h in holdings:
            bucket = h.asset_bucket.value if h.asset_bucket else "UNKNOWN"
            bucket_totals[bucket] = bucket_totals.get(bucket, 0.0) + (h.quantity * h.avg_cost)
        allocation = {k: round(v / invested * 100, 2) for k, v in bucket_totals.items()}

    # Top 5 pending signals with symbol
    top_signals_rows = (
        db.query(Signal, Instrument.symbol)
        .join(Instrument, Signal.instrument_id == Instrument.id)
        .filter(Signal.status == SignalStatus.PENDING)
        .order_by(Signal.ts.desc())
        .limit(5)
        .all()
    )

    return {
        "capital": {
            "total": invested,
            "invested": invested,
            "cash_available": settings.DEFAULT_CAPITAL - invested,
        },
        "daily_pnl": 0.0,
        "risk_status": "SAFE",
        "risk_utilization_pct": 0.0,
        "active_signals": active_signals,
        "todays_orders": todays_orders,
        "pending_reviews": 0,
        "critical_alerts": critical_alerts,
        "broker_connected": False,
        "recent_alerts": [
            {"id": a.id, "severity": a.severity, "message": a.message,
             "category": a.category, "created_at": str(a.created_at)}
            for a in recent_alerts
        ],
        "recent_orders": [
            {"id": o.id, "side": o.side, "status": o.status,
             "quantity": o.quantity, "mode": o.mode, "created_at": str(o.created_at)}
            for o in recent_orders
        ],
        "strategies": [
            {"id": s.id, "name": s.name, "asset_type": s.asset_type, "mode": s.mode}
            for s in active_strategies
        ],
        "portfolio_allocation": allocation,
        "top_signals": [
            {"id": sig.id, "symbol": symbol, "side": sig.side, "confidence": sig.confidence}
            for sig, symbol in top_signals_rows
        ],
        "risk_summary": {
            "capital": settings.DEFAULT_CAPITAL,
            "max_daily_loss": settings.DEFAULT_CAPITAL * settings.MAX_DAILY_LOSS_PCT / 100,
            "paper_mode": settings.PAPER_MODE,
        },
    }


@router.get("/stats")
def get_dashboard_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today_start = datetime.combine(date.today(), datetime.min.time())

    return {
        "total_instruments": db.query(Instrument).filter(Instrument.is_active == True).count(),
        "total_holdings": db.query(Holding).count(),
        "total_signals_today": db.query(Signal).filter(Signal.ts >= today_start).count(),
        "total_orders_today": db.query(Order).filter(Order.created_at >= today_start).count(),
        "pending_signals": db.query(Signal).filter(Signal.status == SignalStatus.PENDING).count(),
        "approved_signals": db.query(Signal).filter(Signal.status == SignalStatus.APPROVED).count(),
    }
