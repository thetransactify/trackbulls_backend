"""
app/api/v1/endpoints/dashboard.py
GET /dashboard/summary — command center KPIs
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from app.db.session import get_db
from app.core.deps import get_current_user
from app.models.models import (
    Holding, Order, Signal, Alert, OrderStatus,
    SignalStatus, AlertSeverity, User
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
    from datetime import date, datetime
    today_start = datetime.combine(date.today(), datetime.min.time())
    todays_orders = db.query(Order).filter(Order.created_at >= today_start).count()

    # Unacknowledged critical alerts
    critical_alerts = db.query(Alert).filter(
        Alert.severity == AlertSeverity.CRITICAL,
        Alert.acknowledged_at == None
    ).count()

    # Recent alerts
    recent_alerts = db.query(Alert).order_by(
        Alert.created_at.desc()
    ).limit(5).all()

    # Recent orders
    recent_orders = db.query(Order).order_by(
        Order.created_at.desc()
    ).limit(5).all()

    return {
        "capital": {
            "total": invested,
            "invested": invested,
            "cash_available": 100000 - invested,  # TODO: link to settings capital
        },
        "daily_pnl": 0.0,            # TODO: compute from filled orders
        "risk_status": "SAFE",        # TODO: link to risk engine
        "risk_utilization_pct": 0.0,
        "active_signals": active_signals,
        "todays_orders": todays_orders,
        "pending_reviews": 0,         # TODO: link to reviews table
        "critical_alerts": critical_alerts,
        "broker_connected": False,    # TODO: check Kite session
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
    }
