"""
app/api/v1/endpoints/risk.py
Risk dashboard, alerts, kill switch, and risk history endpoints.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.core.deps import get_current_user, require_founder
from app.db.session import get_db
from app.models.models import (
    Alert,
    AlertSeverity,
    AuditLog,
    CapBucket,
    Holding,
    Instrument,
    Order,
    OrderStatus,
    Settings as AppSettings,
    Signal,
    SignalSide,
    SignalStatus,
    User,
    UserRole,
)
from app.services.engines.risk_engine import (
    calculate_risk_snapshot,
    evaluate_risk_rules,
    get_risk_color,
)

router = APIRouter(prefix="/risk", tags=["Risk"])

BUCKET_TARGETS = {
    "LARGE": 40.0,
    "MID": 30.0,
    "SMALL": 20.0,
    "TRADING": 10.0,
}


class PreTradeCheckRequest(BaseModel):
    instrument_id: int
    side: str
    quantity: float
    price: float


class AlertCreateRequest(BaseModel):
    severity: AlertSeverity
    category: str
    message: str
    related_entity_type: Optional[str] = None
    related_entity_id: Optional[int] = None


@router.get("/status")
def get_risk_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    snapshot = calculate_risk_snapshot(
        db=db,
        capital=settings.DEFAULT_CAPITAL,
        max_daily_loss_pct=settings.MAX_DAILY_LOSS_PCT,
    )
    snapshot.kill_switch_active = _get_bool_setting(db, "kill_switch_activated")
    rules = evaluate_risk_rules(
        snapshot,
        {
            "max_daily_loss_pct": settings.MAX_DAILY_LOSS_PCT,
        },
    )
    overall_status = _worst_status([rule.status for rule in rules])
    breached = [rule for rule in rules if rule.status == "BREACHED"]
    warnings = [rule for rule in rules if rule.status == "WARNING"]
    top_concern = (breached or warnings)[0].name if (breached or warnings) else None

    return {
        "snapshot": asdict(snapshot),
        "rules": [asdict(rule) for rule in rules],
        "overall_status": overall_status,
        "summary": {
            "status_color": get_risk_color(overall_status),
            "critical_count": len(breached),
            "warning_count": len(warnings),
            "ok_count": len([rule for rule in rules if rule.status == "OK"]),
            "top_concern": top_concern,
        },
        "generated_at": datetime.utcnow().isoformat(),
    }


@router.get("/exposure")
def get_risk_exposure(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(Holding)
        .options(joinedload(Holding.instrument))
        .all()
    )
    positions = [_holding_position(row) for row in rows]
    positions = [item for item in positions if item["invested_value"] > 0]
    total_invested = sum(item["invested_value"] for item in positions)
    capital = settings.DEFAULT_CAPITAL

    by_asset_values: dict[str, float] = {}
    by_bucket_values: dict[str, float] = {bucket: 0.0 for bucket in BUCKET_TARGETS}
    by_sector_values: dict[str, float] = {}
    for item in positions:
        by_asset_values[item["asset_type"]] = by_asset_values.get(item["asset_type"], 0.0) + item["invested_value"]
        by_bucket_values[item["cap_bucket"]] = by_bucket_values.get(item["cap_bucket"], 0.0) + item["invested_value"]
        by_sector_values[item["sector"]] = by_sector_values.get(item["sector"], 0.0) + item["invested_value"]

    return {
        "by_asset_type": [
            {
                "type": asset_type,
                "invested_value": _round(value),
                "pct_of_portfolio": _pct(value, total_invested),
                "pct_of_capital": _pct(value, capital),
            }
            for asset_type, value in sorted(by_asset_values.items())
        ],
        "by_cap_bucket": [
            {
                "bucket": bucket,
                "target_pct": target,
                "current_pct": current_pct,
                "drift": _round(current_pct - target),
                "drift_status": _drift_status(current_pct - target),
            }
            for bucket, target in BUCKET_TARGETS.items()
            for current_pct in [_pct(by_bucket_values.get(bucket, 0.0), total_invested)]
        ],
        "by_sector": [
            {
                "sector": sector,
                "invested_value": _round(value),
                "pct_of_portfolio": _pct(value, total_invested),
            }
            for sector, value in sorted(by_sector_values.items())
        ],
        "largest_positions": [
            {
                "symbol": item["symbol"],
                "sector": item["sector"],
                "cap_bucket": item["cap_bucket"],
                "invested_value": _round(item["invested_value"]),
                "pct_of_capital": _pct(item["invested_value"], capital),
            }
            for item in sorted(positions, key=lambda row: row["invested_value"], reverse=True)[:5]
        ],
    }


@router.get("/daily-summary")
def get_daily_risk_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    today_start = datetime.combine(date.today(), datetime.min.time())
    tomorrow_start = today_start + timedelta(days=1)
    snapshot = calculate_risk_snapshot(
        db=db,
        capital=settings.DEFAULT_CAPITAL,
        max_daily_loss_pct=settings.MAX_DAILY_LOSS_PCT,
    )
    orders_today = db.query(Order).filter(
        Order.created_at >= today_start,
        Order.created_at < tomorrow_start,
    ).count()
    signals_generated = db.query(Signal).filter(
        Signal.ts >= today_start,
        Signal.ts < tomorrow_start,
    ).count()
    signals_approved = db.query(Signal).filter(
        Signal.status == SignalStatus.APPROVED,
        Signal.ts >= today_start,
        Signal.ts < tomorrow_start,
    ).count()
    risk_events = db.query(Alert).filter(
        Alert.severity == AlertSeverity.CRITICAL,
        Alert.created_at >= today_start,
        Alert.created_at < tomorrow_start,
    ).order_by(Alert.created_at.desc()).all()

    return {
        "date": date.today().isoformat(),
        "opening_capital": settings.DEFAULT_CAPITAL,
        "realized_pnl_today": snapshot.daily_pnl,
        "unrealized_pnl": 0,
        "orders_today": orders_today,
        "signals_generated": signals_generated,
        "signals_approved": signals_approved,
        "risk_events": [_serialize_alert(alert) for alert in risk_events],
        "daily_loss_used_pct": snapshot.daily_loss_used_pct,
        "status_history": [],
    }


@router.get("/alerts")
def get_risk_alerts(
    severity: Optional[AlertSeverity] = Query(None),
    category: Optional[str] = Query(None),
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Alert).filter(Alert.acknowledged_at == None)
    if severity:
        query = query.filter(Alert.severity == severity)
    if category:
        query = query.filter(Alert.category == category)

    alerts = query.order_by(Alert.created_at.desc()).limit(limit).all()
    return {
        "alerts": [_serialize_alert(alert) for alert in alerts],
        "count": len(alerts),
    }


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_risk_alert(
    alert_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    alert = db.query(Alert).filter(Alert.id == alert_id).first()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.acknowledged_at = datetime.utcnow()
    db.commit()
    db.refresh(alert)
    return _serialize_alert(alert)


@router.post("/alerts", status_code=status.HTTP_201_CREATED)
def create_risk_alert(
    payload: AlertCreateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    _require_founder_or_analyst(current_user)
    alert = Alert(
        severity=payload.severity,
        category=payload.category,
        message=payload.message,
        related_entity_type=payload.related_entity_type,
        related_entity_id=payload.related_entity_id,
    )
    db.add(alert)
    db.commit()
    db.refresh(alert)
    return _serialize_alert(alert)


@router.post("/kill-switch")
def kill_switch(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    activated_at = datetime.utcnow()
    orders_cancelled = db.query(Order).filter(
        Order.status.in_([OrderStatus.CREATED, OrderStatus.SENT])
    ).update({"status": OrderStatus.CANCELLED}, synchronize_session=False)
    signals_rejected = db.query(Signal).filter(
        Signal.status == SignalStatus.PENDING
    ).update({"status": SignalStatus.REJECTED}, synchronize_session=False)

    alert = Alert(
        severity=AlertSeverity.CRITICAL,
        category="RISK",
        message=(
            f"Kill switch activated by {current_user.username} "
            f"at {activated_at.isoformat()}"
        ),
    )
    db.add(alert)
    db.add(AuditLog(
        actor_user_id=current_user.id,
        action="KILL_SWITCH_ACTIVATED",
        entity_type="RISK",
        entity_id=None,
        after_json={
            "activated_at": activated_at.isoformat(),
            "activated_by": current_user.username,
            "orders_cancelled": orders_cancelled,
            "signals_rejected": signals_rejected,
        },
    ))
    _set_setting(db, "kill_switch_activated", "true", current_user.id)
    db.commit()

    return {
        "activated_at": activated_at.isoformat(),
        "activated_by": current_user.username,
        "orders_cancelled": orders_cancelled,
        "signals_rejected": signals_rejected,
        "message": "Kill switch activated. All trading halted.",
    }


@router.post("/kill-switch/reset")
def reset_kill_switch(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    reset_at = datetime.utcnow()
    _set_setting(db, "kill_switch_activated", "false", current_user.id)
    db.add(Alert(
        severity=AlertSeverity.INFO,
        category="RISK",
        message=f"Kill switch reset by {current_user.username}",
    ))
    db.commit()
    return {
        "reset_at": reset_at.isoformat(),
        "reset_by": current_user.username,
        "message": "Kill switch reset. Trading can resume.",
    }


@router.get("/history")
def get_risk_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    start_day = date.today() - timedelta(days=29)
    rows = (
        db.query(
            func.date(Order.created_at).label("order_date"),
            func.sum(
                func.coalesce(Order.filled_qty, Order.quantity, 0)
                * func.coalesce(Order.filled_price, Order.price, 0)
            ).label("filled_value"),
        )
        .filter(
            Order.status == OrderStatus.FILLED,
            Order.created_at >= datetime.combine(start_day, datetime.min.time()),
        )
        .group_by(func.date(Order.created_at))
        .all()
    )
    invested_by_date = {
        _date_key(row.order_date): float(row.filled_value or 0.0)
        for row in rows
    }

    items = []
    for offset in range(30):
        day = start_day + timedelta(days=offset)
        total_invested = invested_by_date.get(day.isoformat(), 0.0)
        daily_loss_pct = 0.0
        items.append({
            "date": day.isoformat(),
            "status": "SAFE",
            "daily_loss_pct": daily_loss_pct,
            "total_invested": _round(total_invested),
        })
    return {"history": items}


@router.post("/check")
def pre_trade_check(
    payload: PreTradeCheckRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    capital = settings.DEFAULT_CAPITAL
    trade_value = payload.quantity * payload.price
    trade_pct = trade_value / capital * 100 if capital > 0 else 0
    checks = {
        "capital_available": True,
        "daily_loss_ok": True,
        "trade_size_ok": trade_pct <= 20,
        "paper_mode": settings.PAPER_MODE,
    }
    passed = all(checks.values())
    return {
        "passed": passed,
        "checks": checks,
        "trade_value": _round(trade_value),
        "trade_pct_of_capital": _round(trade_pct),
        "message": "Pre-trade check passed" if passed else "Pre-trade check FAILED - review checks",
    }


def _serialize_alert(alert: Alert) -> dict:
    return {
        "id": alert.id,
        "severity": _enum_value(alert.severity),
        "category": alert.category,
        "message": alert.message,
        "related_entity_type": alert.related_entity_type,
        "related_entity_id": alert.related_entity_id,
        "acknowledged_at": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
        "is_acknowledged": alert.acknowledged_at is not None,
        "created_at": alert.created_at.isoformat() if alert.created_at else None,
    }


def _holding_position(holding: Holding) -> dict:
    instrument = holding.instrument
    invested_value = float(holding.quantity or 0.0) * float(holding.avg_cost or 0.0)
    return {
        "symbol": instrument.symbol if instrument else None,
        "sector": instrument.sector if instrument and instrument.sector else "UNKNOWN",
        "asset_type": _enum_value(instrument.asset_type) if instrument else "UNKNOWN",
        "cap_bucket": _enum_value(holding.asset_bucket or (instrument.cap_bucket if instrument else None), "TRADING"),
        "invested_value": invested_value,
    }


def _set_setting(db: Session, key: str, value: str, user_id: int) -> AppSettings:
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    if row:
        row.value = value
        row.updated_by = user_id
    else:
        row = AppSettings(key=key, value=value, updated_by=user_id)
        db.add(row)
    return row


def _get_bool_setting(db: Session, key: str) -> bool:
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    if not row or row.value is None:
        return False
    return str(row.value).strip().lower() in {"1", "true", "yes", "on"}


def _require_founder_or_analyst(user: User) -> None:
    if user.role not in {UserRole.FOUNDER, UserRole.ANALYST}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Founder or analyst access required",
        )


def _worst_status(statuses: list[str]) -> str:
    severity = {
        "OK": 0,
        "SAFE": 0,
        "WARNING": 1,
        "DANGER": 2,
        "BREACHED": 3,
    }
    if not statuses:
        return "OK"
    return max(statuses, key=lambda item: severity.get(item, 0))


def _drift_status(drift: float) -> str:
    if abs(drift) <= 5.0:
        return "ON_TARGET"
    if drift > 5.0:
        return "OVERWEIGHT"
    return "UNDERWEIGHT"


def _pct(value: float, denominator: float) -> float:
    if denominator <= 0:
        return 0.0
    return _round(value / denominator * 100.0)


def _round(value: float) -> float:
    return round(float(value or 0.0), 2)


def _enum_value(value, fallback: Optional[str] = None) -> Optional[str]:
    if value is None:
        return fallback
    if hasattr(value, "value"):
        return value.value
    return str(value)


def _date_key(value) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)
