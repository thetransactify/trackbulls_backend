"""
app/api/v1/endpoints/portfolio.py
GET  /portfolio                  — all holdings + full summary + allocation
GET  /portfolio/holding/{id}     — single holding detail
POST /portfolio/holding          — add holding manually
PUT  /portfolio/holding/{id}     — update holding fields
DEL  /portfolio/holding/{id}     — remove holding
GET  /portfolio/allocation       — current vs target allocation with drift
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session, joinedload
from pydantic import BaseModel, field_validator
from typing import Optional
from app.db.session import get_db
from app.core.deps import get_current_user
from app.models.models import Holding, Instrument, User, CapBucket

router = APIRouter(prefix="/portfolio", tags=["Portfolio"])

TARGET_ALLOCATION = {
    CapBucket.LARGE:   40.0,
    CapBucket.MID:     30.0,
    CapBucket.SMALL:   20.0,
    CapBucket.TRADING: 10.0,
}


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class HoldingCreate(BaseModel):
    instrument_id: int
    quantity: float
    avg_cost: float
    asset_bucket: CapBucket
    notes: Optional[str] = None

    @field_validator("quantity")
    @classmethod
    def qty_positive(cls, v):
        if v <= 0:
            raise ValueError("quantity must be > 0")
        return v

    @field_validator("avg_cost")
    @classmethod
    def cost_positive(cls, v):
        if v <= 0:
            raise ValueError("avg_cost must be > 0")
        return v


class HoldingUpdate(BaseModel):
    quantity:      Optional[float] = None
    avg_cost:      Optional[float] = None
    notes:         Optional[str]   = None
    thesis_status: Optional[str]   = None

    @field_validator("quantity")
    @classmethod
    def qty_positive(cls, v):
        if v is not None and v <= 0:
            raise ValueError("quantity must be > 0")
        return v

    @field_validator("avg_cost")
    @classmethod
    def cost_positive(cls, v):
        if v is not None and v <= 0:
            raise ValueError("avg_cost must be > 0")
        return v


# ── helpers ───────────────────────────────────────────────────────────────────

def _holding_dict(h: Holding) -> dict:
    instr: Instrument = h.instrument
    invested = h.quantity * h.avg_cost
    return {
        "id":             h.id,
        "symbol":         instr.symbol   if instr else "UNKNOWN",
        "exchange":       instr.exchange if instr else "",
        "asset_type":     instr.asset_type.value if instr and instr.asset_type else "",
        "sector":         instr.sector   if instr else None,
        "cap_bucket":     instr.cap_bucket.value if instr and instr.cap_bucket else None,
        "quantity":       h.quantity,
        "avg_cost":       h.avg_cost,
        "invested_value": invested,
        "current_value":  invested,       # Phase 2: replace with live price
        "pnl":            0.0,
        "pnl_pct":        0.0,
        "asset_bucket":   h.asset_bucket.value if h.asset_bucket else None,
        "thesis_status":  h.thesis_status,
        "last_review":    str(h.last_review) if h.last_review else None,
        "notes":          h.notes,
        "created_at":     str(h.created_at) if h.created_at else None,
    }


def _build_allocation(holdings_data: list[dict], total_invested: float) -> dict:
    """Return per-bucket absolute values and percentages."""
    values: dict[str, float] = {b.value: 0.0 for b in CapBucket}
    for h in holdings_data:
        bucket = h.get("asset_bucket")
        if bucket and bucket in values:
            values[bucket] += h["invested_value"]

    pcts: dict[str, float] = {}
    for bucket, val in values.items():
        pcts[bucket] = round(val / total_invested * 100, 2) if total_invested else 0.0

    return {"values": values, "percentages": pcts}


# ── endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def get_portfolio(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    holdings = (
        db.query(Holding)
        .options(joinedload(Holding.instrument))
        .all()
    )

    result = [_holding_dict(h) for h in holdings]
    total_invested = sum(h["invested_value"] for h in result)

    # Cap-bucket totals for summary percentages
    bucket_totals: dict[str, float] = {b.value: 0.0 for b in CapBucket}
    for h in result:
        b = h.get("asset_bucket")
        if b and b in bucket_totals:
            bucket_totals[b] += h["invested_value"]

    def _pct(bucket_key: str) -> float:
        return round(bucket_totals[bucket_key] / total_invested * 100, 2) if total_invested else 0.0

    allocation = _build_allocation(result, total_invested)

    return {
        "holdings": result,
        "summary": {
            "total_invested":       total_invested,
            "total_current_value":  total_invested,
            "total_pnl":            0.0,
            "total_pnl_pct":        0.0,
            "holdings_count":       len(result),
            "large_cap_pct":        _pct("LARGE"),
            "mid_cap_pct":          _pct("MID"),
            "small_cap_pct":        _pct("SMALL"),
            "trading_pct":          _pct("TRADING"),
        },
        "allocation": allocation,
    }


@router.get("/allocation")
def get_allocation(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    holdings = (
        db.query(Holding)
        .options(joinedload(Holding.instrument))
        .all()
    )

    result = [_holding_dict(h) for h in holdings]
    total_invested = sum(h["invested_value"] for h in result)
    alloc = _build_allocation(result, total_invested)
    current_pcts = alloc["percentages"]

    drift = {
        b.value: round(current_pcts.get(b.value, 0.0) - TARGET_ALLOCATION[b], 2)
        for b in CapBucket
    }

    return {
        "current": current_pcts,
        "target":  {b.value: TARGET_ALLOCATION[b] for b in CapBucket},
        "drift":   drift,
    }


@router.get("/holding/{holding_id}")
def get_holding(
    holding_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    holding = (
        db.query(Holding)
        .options(joinedload(Holding.instrument))
        .filter(Holding.id == holding_id)
        .first()
    )
    if not holding:
        raise HTTPException(status_code=404, detail="Holding not found")
    return _holding_dict(holding)


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

    # Re-query with relationship loaded for full response
    holding = (
        db.query(Holding)
        .options(joinedload(Holding.instrument))
        .filter(Holding.id == holding.id)
        .first()
    )
    return _holding_dict(holding)


@router.put("/holding/{holding_id}")
def update_holding(
    holding_id: int,
    payload: HoldingUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    holding = (
        db.query(Holding)
        .options(joinedload(Holding.instrument))
        .filter(Holding.id == holding_id)
        .first()
    )
    if not holding:
        raise HTTPException(status_code=404, detail="Holding not found")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(holding, field, value)

    db.commit()
    db.refresh(holding)
    return _holding_dict(holding)


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
