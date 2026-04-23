"""
app/api/v1/endpoints/instruments.py
GET  /instruments/search?q=          — global search (auth)
GET  /instruments/screener            — equity screener with filters (auth)
GET  /instruments/sectors             — distinct sector list (no auth)
GET  /instruments/mcx                 — all MCX instruments (auth)
GET  /instruments/{symbol}            — instrument detail (auth)
POST /instruments/{id}/fundamentals   — add fundamentals snapshot (founder)
POST /instruments                     — add instrument (founder)

IMPORTANT: all static paths (/search, /screener, /sectors, /mcx) are
registered BEFORE /{symbol} to prevent path-param capture.
"""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from app.db.session import get_db
from app.core.deps import get_current_user, require_founder
from app.models.models import (
    Instrument, Score, FundamentalsSnapshot, Holding,
    User, AssetType, CapBucket,
)

router = APIRouter(prefix="/instruments", tags=["Instruments"])


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class InstrumentCreate(BaseModel):
    symbol:     str
    exchange:   str
    asset_type: AssetType
    sector:     Optional[str]      = None
    cap_bucket: Optional[CapBucket] = None


class FundamentalsCreate(BaseModel):
    pe:          Optional[float]    = None
    roe:         Optional[float]    = None
    eps:         Optional[float]    = None
    debt_equity: Optional[float]    = None
    fii_pct:     Optional[float]    = None
    dii_pct:     Optional[float]    = None
    sales:       Optional[float]    = None
    profit:      Optional[float]    = None
    market_cap:  Optional[float]    = None
    as_of_date:  Optional[datetime] = None


# ── helpers ───────────────────────────────────────────────────────────────────

def _fund_dict(f: FundamentalsSnapshot | None) -> dict | None:
    if not f:
        return None
    return {
        "pe":          f.pe,
        "roe":         f.roe,
        "eps":         f.eps,
        "debt_equity": f.debt_equity,
        "fii_pct":     f.fii_pct,
        "dii_pct":     f.dii_pct,
        "sales":       f.sales,
        "profit":      f.profit,
        "market_cap":  f.market_cap,
        "as_of_date":  str(f.as_of_date) if f.as_of_date else None,
    }


def _screener_row(inst: Instrument, score: Score | None, fund: FundamentalsSnapshot | None) -> dict:
    return {
        "id":               inst.id,
        "symbol":           inst.symbol,
        "exchange":         inst.exchange,
        "asset_type":       inst.asset_type.value if inst.asset_type else None,
        "sector":           inst.sector,
        "cap_bucket":       inst.cap_bucket.value if inst.cap_bucket else None,
        "score":            score.score_value if score else 0.0,
        "band":             score.band.value if score and score.band else None,
        "last_scored_at":   str(score.ts) if score else None,
        "pe":               fund.pe if fund else None,
        "roe":              fund.roe if fund else None,
        "eps":              fund.eps if fund else None,
        "debt_equity":      fund.debt_equity if fund else None,
        "fii_pct":          fund.fii_pct if fund else None,
        "dii_pct":          fund.dii_pct if fund else None,
        "sales":            fund.sales if fund else None,
        "profit":           fund.profit if fund else None,
        "market_cap":       fund.market_cap if fund else None,
        "has_fundamentals": fund is not None,
    }


def _latest_score(db: Session, instrument_id: int) -> Score | None:
    return (
        db.query(Score)
        .filter(Score.instrument_id == instrument_id)
        .order_by(Score.ts.desc())
        .first()
    )


def _latest_fund(db: Session, instrument_id: int) -> FundamentalsSnapshot | None:
    return (
        db.query(FundamentalsSnapshot)
        .filter(FundamentalsSnapshot.instrument_id == instrument_id)
        .order_by(FundamentalsSnapshot.as_of_date.desc())
        .first()
    )


# ── GET /search ───────────────────────────────────────────────────────────────

@router.get("/search")
def search_instruments(
    q: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    results = (
        db.query(Instrument)
        .filter(Instrument.symbol.ilike(f"%{q.upper()}%"), Instrument.is_active == True)
        .limit(20)
        .all()
    )
    return {
        "query": q,
        "results": [
            {
                "id":         i.id,
                "symbol":     i.symbol,
                "exchange":   i.exchange,
                "asset_type": i.asset_type.value if i.asset_type else None,
                "sector":     i.sector,
                "cap_bucket": i.cap_bucket.value if i.cap_bucket else None,
            }
            for i in results
        ],
    }


# ── GET /screener ─────────────────────────────────────────────────────────────

@router.get("/screener")
def equity_screener(
    asset_type: Optional[str]  = Query("EQUITY", description="EQUITY | MCX"),
    cap_bucket: Optional[str]  = Query(None,     description="LARGE | MID | SMALL"),
    sector:     Optional[str]  = Query(None),
    min_score:  float          = Query(0.0),
    max_pe:     Optional[float] = Query(None),
    min_roe:    Optional[float] = Query(None),
    max_debt:   Optional[float] = Query(None),
    sort_by:    str            = Query("score",  description="score | pe | roe | symbol"),
    limit:      int            = Query(50, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Instrument).filter(Instrument.is_active == True)

    # asset_type filter
    try:
        q = q.filter(Instrument.asset_type == AssetType(asset_type.upper()))
    except ValueError:
        raise HTTPException(status_code=422, detail=f"Invalid asset_type: {asset_type}")

    if cap_bucket:
        try:
            q = q.filter(Instrument.cap_bucket == CapBucket(cap_bucket.upper()))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid cap_bucket: {cap_bucket}")

    if sector:
        q = q.filter(Instrument.sector.ilike(f"%{sector}%"))

    instruments = q.limit(limit * 4).all()  # over-fetch then filter by fundamentals
    result = []

    for inst in instruments:
        score = _latest_score(db, inst.id)
        fund  = _latest_fund(db, inst.id)

        score_val = score.score_value if score else 0.0
        if score_val < min_score:
            continue
        if max_pe is not None and (fund is None or fund.pe is None or fund.pe > max_pe):
            continue
        if min_roe is not None and (fund is None or fund.roe is None or fund.roe < min_roe):
            continue
        if max_debt is not None and (fund is None or fund.debt_equity is None or fund.debt_equity > max_debt):
            continue

        result.append(_screener_row(inst, score, fund))

    # sorting
    if sort_by == "pe":
        result.sort(key=lambda x: (x["pe"] is None, x["pe"] or 0))
    elif sort_by == "roe":
        result.sort(key=lambda x: (x["roe"] is None, -(x["roe"] or 0)))
    elif sort_by == "symbol":
        result.sort(key=lambda x: x["symbol"])
    else:  # default: score desc
        result.sort(key=lambda x: x["score"], reverse=True)

    return {"count": len(result[:limit]), "instruments": result[:limit]}


# ── GET /sectors (no auth) ────────────────────────────────────────────────────

@router.get("/sectors")
def get_sectors(db: Session = Depends(get_db)):
    rows = (
        db.query(Instrument.sector)
        .filter(Instrument.sector.isnot(None), Instrument.is_active == True)
        .distinct()
        .all()
    )
    return {"sectors": sorted(r[0] for r in rows if r[0])}


# ── GET /mcx ──────────────────────────────────────────────────────────────────

@router.get("/mcx")
def get_mcx_instruments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    instruments = (
        db.query(Instrument)
        .filter(Instrument.asset_type == AssetType.MCX, Instrument.is_active == True)
        .all()
    )
    result = [_screener_row(inst, _latest_score(db, inst.id), _latest_fund(db, inst.id))
              for inst in instruments]
    return {"count": len(result), "instruments": result}


# ── GET /{symbol} ─────────────────────────────────────────────────────────────

@router.get("/{symbol}")
def get_instrument_detail(
    symbol: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    inst = db.query(Instrument).filter(
        Instrument.symbol == symbol.upper(),
        Instrument.is_active == True,
    ).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instrument not found")

    scores = (
        db.query(Score)
        .filter(Score.instrument_id == inst.id)
        .order_by(Score.ts.desc())
        .limit(10)
        .all()
    )

    fund = _latest_fund(db, inst.id)

    holding = (
        db.query(Holding)
        .filter(Holding.instrument_id == inst.id)
        .first()
    )

    return {
        "id":           inst.id,
        "symbol":       inst.symbol,
        "exchange":     inst.exchange,
        "asset_type":   inst.asset_type.value if inst.asset_type else None,
        "sector":       inst.sector,
        "cap_bucket":   inst.cap_bucket.value if inst.cap_bucket else None,
        "is_active":    inst.is_active,
        "created_at":   str(inst.created_at) if inst.created_at else None,
        "latest_score": {
            "value":   scores[0].score_value if scores else None,
            "band":    scores[0].band.value if scores and scores[0].band else None,
            "factors": scores[0].factors_json if scores else {},
            "ts":      str(scores[0].ts) if scores else None,
        } if scores else None,
        "fundamentals": _fund_dict(fund),
        "score_history": [
            {"value": s.score_value, "band": s.band.value if s.band else None, "ts": str(s.ts)}
            for s in scores
        ],
        "in_portfolio":   holding is not None,
        "holding_detail": {
            "id":             holding.id,
            "quantity":       holding.quantity,
            "avg_cost":       holding.avg_cost,
            "invested_value": holding.quantity * holding.avg_cost,
            "asset_bucket":   holding.asset_bucket.value if holding.asset_bucket else None,
            "thesis_status":  holding.thesis_status,
            "notes":          holding.notes,
        } if holding else None,
    }


# ── POST /{id}/fundamentals ───────────────────────────────────────────────────

@router.post("/{instrument_id}/fundamentals", status_code=201)
def add_fundamentals(
    instrument_id: int,
    payload: FundamentalsCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    inst = db.query(Instrument).filter(Instrument.id == instrument_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instrument not found")

    snap = FundamentalsSnapshot(
        instrument_id=instrument_id,
        as_of_date=payload.as_of_date or datetime.now(tz=timezone.utc),
        pe=payload.pe,
        roe=payload.roe,
        eps=payload.eps,
        debt_equity=payload.debt_equity,
        fii_pct=payload.fii_pct,
        dii_pct=payload.dii_pct,
        sales=payload.sales,
        profit=payload.profit,
        market_cap=payload.market_cap,
    )
    db.add(snap)
    db.commit()
    db.refresh(snap)

    return {
        "id":            snap.id,
        "instrument_id": snap.instrument_id,
        "symbol":        inst.symbol,
        "as_of_date":    str(snap.as_of_date),
        **_fund_dict(snap),
    }


# ── POST / (add instrument) ───────────────────────────────────────────────────

@router.post("", status_code=201)
def add_instrument(
    payload: InstrumentCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    exists = db.query(Instrument).filter(
        Instrument.symbol == payload.symbol.upper(),
        Instrument.exchange == payload.exchange.upper(),
    ).first()
    if exists:
        raise HTTPException(status_code=400, detail="Instrument already exists")

    inst = Instrument(
        symbol=payload.symbol.upper(),
        exchange=payload.exchange.upper(),
        asset_type=payload.asset_type,
        sector=payload.sector,
        cap_bucket=payload.cap_bucket,
    )
    db.add(inst)
    db.commit()
    db.refresh(inst)
    return {"id": inst.id, "symbol": inst.symbol, "message": "Instrument added"}
