"""
app/api/v1/endpoints/portfolio.py
GET  /portfolio          — all holdings + allocation summary
POST /portfolio/holding  — add holding manually
PUT  /portfolio/holding/{id}
DEL  /portfolio/holding/{id}
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from app.db.session import get_db
from app.core.deps import get_current_user
from app.models.models import Holding, Instrument, User, CapBucket

router = APIRouter(prefix="/portfolio", tags=["Portfolio"])


class HoldingCreate(BaseModel):
    instrument_id: int
    quantity: float
    avg_cost: float
    asset_bucket: CapBucket
    notes: Optional[str] = None


@router.get("")
def get_portfolio(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    holdings = db.query(Holding).all()
    result = []
    total_invested = 0.0

    for h in holdings:
        instrument = db.query(Instrument).filter(Instrument.id == h.instrument_id).first()
        value = h.quantity * h.avg_cost
        total_invested += value
        result.append({
            "id": h.id,
            "symbol": instrument.symbol if instrument else "UNKNOWN",
            "exchange": instrument.exchange if instrument else "",
            "asset_type": instrument.asset_type if instrument else "",
            "quantity": h.quantity,
            "avg_cost": h.avg_cost,
            "invested_value": value,
            "current_value": value,    # TODO: plug in live price feed
            "pnl": 0.0,
            "pnl_pct": 0.0,
            "asset_bucket": h.asset_bucket,
            "thesis_status": h.thesis_status,
            "last_review": str(h.last_review) if h.last_review else None,
        })

    # Allocation breakdown by bucket
    allocation = {}
    for h_data in result:
        bucket = str(h_data["asset_bucket"])
        allocation[bucket] = allocation.get(bucket, 0) + h_data["invested_value"]

    return {
        "summary": {
            "total_invested": total_invested,
            "total_current_value": total_invested,
            "total_pnl": 0.0,
            "total_pnl_pct": 0.0,
            "holdings_count": len(result),
        },
        "allocation": allocation,
        "holdings": result,
    }


@router.post("/holding", status_code=201)
def add_holding(
    payload: HoldingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    instrument = db.query(Instrument).filter(Instrument.id == payload.instrument_id).first()
    if not instrument:
        raise HTTPException(status_code=404, detail="Instrument not found")

    holding = Holding(**payload.model_dump())
    db.add(holding)
    db.commit()
    db.refresh(holding)
    return {"id": holding.id, "message": "Holding added"}


@router.delete("/holding/{holding_id}")
def remove_holding(
    holding_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    holding = db.query(Holding).filter(Holding.id == holding_id).first()
    if not holding:
        raise HTTPException(status_code=404, detail="Holding not found")
    db.delete(holding)
    db.commit()
    return {"message": "Holding removed"}
