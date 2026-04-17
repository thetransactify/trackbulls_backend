"""
app/api/v1/endpoints/reports.py
GET /reports/daily   — daily performance summary
GET /reports/monthly — monthly performance summary
"""
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from datetime import date, datetime, timedelta
from app.db.session import get_db
from app.core.deps import get_current_user
from app.models.models import Order, Signal, Alert, OrderStatus, SignalStatus, User

router = APIRouter(prefix="/reports", tags=["Reports"])


@router.get("/daily")
def daily_report(
    report_date: str = Query(None, description="YYYY-MM-DD, defaults to today"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if report_date:
        day = datetime.strptime(report_date, "%Y-%m-%d").date()
    else:
        day = date.today()

    start = datetime.combine(day, datetime.min.time())
    end   = datetime.combine(day, datetime.max.time())

    orders = db.query(Order).filter(
        Order.created_at >= start,
        Order.created_at <= end,
    ).all()

    filled   = [o for o in orders if o.status == OrderStatus.FILLED]
    rejected = [o for o in orders if o.status == OrderStatus.REJECTED]

    signals_today = db.query(Signal).filter(
        Signal.ts >= start,
        Signal.ts <= end,
    ).all()

    alerts_today = db.query(Alert).filter(
        Alert.created_at >= start,
        Alert.created_at <= end,
    ).all()

    # TODO: compute real P&L from filled orders + price data
    realized_pnl = 0.0
    win_trades = 0
    loss_trades = 0
    win_rate = 0.0
    if len(filled) > 0:
        win_rate = round(win_trades / len(filled) * 100, 2)

    return {
        "date": str(day),
        "summary": {
            "total_orders": len(orders),
            "filled_orders": len(filled),
            "rejected_orders": len(rejected),
            "cancelled_orders": len([o for o in orders if o.status.value == "CANCELLED"]),
            "total_signals": len(signals_today),
            "approved_signals": len([s for s in signals_today if s.status == SignalStatus.APPROVED]),
            "total_alerts": len(alerts_today),
        },
        "performance": {
            "realized_pnl": realized_pnl,
            "win_trades": win_trades,
            "loss_trades": loss_trades,
            "win_rate_pct": win_rate,
            "drawdown": 0.0,  # TODO
        },
        "orders": [
            {"id": o.id, "uid": o.uid, "side": o.side, "mode": o.mode,
             "status": o.status, "quantity": o.quantity, "price": o.price}
            for o in orders
        ],
    }


@router.get("/monthly")
def monthly_report(
    year: int = Query(None),
    month: int = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today = date.today()
    y = year or today.year
    m = month or today.month

    start = datetime(y, m, 1)
    if m == 12:
        end = datetime(y + 1, 1, 1) - timedelta(seconds=1)
    else:
        end = datetime(y, m + 1, 1) - timedelta(seconds=1)

    orders = db.query(Order).filter(
        Order.created_at >= start,
        Order.created_at <= end,
    ).all()
    filled = [o for o in orders if o.status == OrderStatus.FILLED]

    # Daily breakdown
    daily: dict = {}
    for o in filled:
        day_key = o.created_at.strftime("%Y-%m-%d") if o.created_at else "unknown"
        daily[day_key] = daily.get(day_key, 0) + 1

    return {
        "period": f"{y}-{m:02d}",
        "summary": {
            "total_orders": len(orders),
            "filled_orders": len(filled),
            "total_realized_pnl": 0.0,   # TODO
            "win_rate_pct": 0.0,           # TODO
            "max_drawdown": 0.0,           # TODO
        },
        "daily_activity": daily,
        "asset_breakdown": {},             # TODO: by asset type
    }
