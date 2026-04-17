"""
app/api/v1/endpoints/signals.py
GET  /signals/equity      — equity swing signals
GET  /signals/mcx         — MCX commodity signals
GET  /signals/{id}        — signal detail
POST /signals/{id}/approve — approve signal for order creation
POST /signals/{id}/reject
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional
from app.db.session import get_db
from app.core.deps import get_current_user, require_trader_or_above
from app.models.models import Signal, Instrument, User, AssetType, SignalStatus, SignalSide

router = APIRouter(prefix="/signals", tags=["Signals"])


def _signal_to_dict(s: Signal, instrument: Instrument) -> dict:
    return {
        "id": s.id,
        "symbol": instrument.symbol if instrument else "",
        "exchange": instrument.exchange if instrument else "",
        "asset_type": instrument.asset_type if instrument else "",
        "side": s.side,
        "target_pct": s.target_pct,
        "stop_pct": s.stop_pct,
        "confidence": s.confidence,
        "status": s.status,
        "reasons": s.reasons_json,
        "review_date": str(s.review_date) if s.review_date else None,
        "created_at": str(s.ts),
    }


@router.get("/equity")
def get_equity_signals(
    side: Optional[str] = Query(None, description="BUY | SELL | HOLD"),
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Signal).join(Instrument).filter(
        Instrument.asset_type == AssetType.EQUITY
    )
    if side:
        query = query.filter(Signal.side == side.upper())
    if status:
        query = query.filter(Signal.status == status.upper())

    signals = query.order_by(Signal.ts.desc()).limit(50).all()
    result = []
    for s in signals:
        instrument = db.query(Instrument).filter(Instrument.id == s.instrument_id).first()
        result.append(_signal_to_dict(s, instrument))
    return {"count": len(result), "signals": result}


@router.get("/mcx")
def get_mcx_signals(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    signals = db.query(Signal).join(Instrument).filter(
        Instrument.asset_type == AssetType.MCX
    ).order_by(Signal.ts.desc()).limit(20).all()

    result = []
    for s in signals:
        instrument = db.query(Instrument).filter(Instrument.id == s.instrument_id).first()
        result.append(_signal_to_dict(s, instrument))
    return {"count": len(result), "signals": result}


@router.get("/{signal_id}")
def get_signal(
    signal_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    s = db.query(Signal).filter(Signal.id == signal_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Signal not found")
    instrument = db.query(Instrument).filter(Instrument.id == s.instrument_id).first()
    return _signal_to_dict(s, instrument)


@router.post("/{signal_id}/approve")
def approve_signal(
    signal_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_trader_or_above),
):
    s = db.query(Signal).filter(Signal.id == signal_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Signal not found")
    if s.status != SignalStatus.PENDING:
        raise HTTPException(status_code=400, detail=f"Signal is already {s.status}")

    s.status = SignalStatus.APPROVED
    s.approved_by = current_user.id
    db.commit()
    return {"message": "Signal approved", "signal_id": signal_id}


@router.post("/{signal_id}/reject")
def reject_signal(
    signal_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_trader_or_above),
):
    s = db.query(Signal).filter(Signal.id == signal_id).first()
    if not s:
        raise HTTPException(status_code=404, detail="Signal not found")

    s.status = SignalStatus.REJECTED
    db.commit()
    return {"message": "Signal rejected", "signal_id": signal_id}
