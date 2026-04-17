"""
app/api/v1/endpoints/instruments.py
GET /instruments/search?q=    — global search
GET /instruments/screener     — equity screener with filters
GET /instruments/{symbol}     — instrument detail
POST /instruments             — add instrument (founder only)
"""
from fastapi import APIRouter, Depends, Query, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from app.db.session import get_db
from app.core.deps import get_current_user, require_founder
from app.models.models import Instrument, Score, FundamentalsSnapshot, User, AssetType, CapBucket

router = APIRouter(prefix="/instruments", tags=["Instruments"])


class InstrumentCreate(BaseModel):
    symbol: str
    exchange: str
    asset_type: AssetType
    sector: Optional[str] = None
    cap_bucket: Optional[CapBucket] = None


@router.get("/search")
def search_instruments(
    q: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    results = db.query(Instrument).filter(
        Instrument.symbol.ilike(f"%{q.upper()}%"),
        Instrument.is_active == True,
    ).limit(20).all()
    return {
        "query": q,
        "results": [
            {"id": i.id, "symbol": i.symbol, "exchange": i.exchange,
             "asset_type": i.asset_type, "sector": i.sector, "cap_bucket": i.cap_bucket}
            for i in results
        ],
    }


@router.get("/screener")
def equity_screener(
    cap_bucket: Optional[str] = Query(None, description="LARGE | MID | SMALL"),
    min_score: float = Query(0.0),
    max_pe: Optional[float] = Query(None),
    min_roe: Optional[float] = Query(None),
    sector: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Instrument).filter(
        Instrument.asset_type == AssetType.EQUITY,
        Instrument.is_active == True,
    )
    if cap_bucket:
        query = query.filter(Instrument.cap_bucket == cap_bucket.upper())
    if sector:
        query = query.filter(Instrument.sector.ilike(f"%{sector}%"))

    instruments = query.limit(limit).all()
    result = []

    for inst in instruments:
        # Get latest score
        latest_score = db.query(Score).filter(
            Score.instrument_id == inst.id
        ).order_by(Score.ts.desc()).first()

        # Get latest fundamentals
        latest_fund = db.query(FundamentalsSnapshot).filter(
            FundamentalsSnapshot.instrument_id == inst.id
        ).order_by(FundamentalsSnapshot.as_of_date.desc()).first()

        score_val = latest_score.score_value if latest_score else 0.0
        if score_val < min_score:
            continue
        if max_pe and latest_fund and latest_fund.pe and latest_fund.pe > max_pe:
            continue
        if min_roe and latest_fund and latest_fund.roe and latest_fund.roe < min_roe:
            continue

        result.append({
            "id": inst.id,
            "symbol": inst.symbol,
            "exchange": inst.exchange,
            "sector": inst.sector,
            "cap_bucket": inst.cap_bucket,
            "score": score_val,
            "band": latest_score.band if latest_score else None,
            "pe": latest_fund.pe if latest_fund else None,
            "roe": latest_fund.roe if latest_fund else None,
            "eps": latest_fund.eps if latest_fund else None,
            "debt_equity": latest_fund.debt_equity if latest_fund else None,
            "fii_pct": latest_fund.fii_pct if latest_fund else None,
            "dii_pct": latest_fund.dii_pct if latest_fund else None,
        })

    result.sort(key=lambda x: x["score"], reverse=True)
    return {"count": len(result), "instruments": result}


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

    scores = db.query(Score).filter(
        Score.instrument_id == inst.id
    ).order_by(Score.ts.desc()).limit(5).all()

    fundamentals = db.query(FundamentalsSnapshot).filter(
        FundamentalsSnapshot.instrument_id == inst.id
    ).order_by(FundamentalsSnapshot.as_of_date.desc()).first()

    return {
        "id": inst.id,
        "symbol": inst.symbol,
        "exchange": inst.exchange,
        "asset_type": inst.asset_type,
        "sector": inst.sector,
        "cap_bucket": inst.cap_bucket,
        "latest_score": {
            "value": scores[0].score_value if scores else None,
            "band": scores[0].band if scores else None,
            "factors": scores[0].factors_json if scores else {},
            "ts": str(scores[0].ts) if scores else None,
        },
        "fundamentals": {
            "pe": fundamentals.pe if fundamentals else None,
            "roe": fundamentals.roe if fundamentals else None,
            "eps": fundamentals.eps if fundamentals else None,
            "debt_equity": fundamentals.debt_equity if fundamentals else None,
            "fii_pct": fundamentals.fii_pct if fundamentals else None,
            "dii_pct": fundamentals.dii_pct if fundamentals else None,
            "sales": fundamentals.sales if fundamentals else None,
            "profit": fundamentals.profit if fundamentals else None,
            "as_of_date": str(fundamentals.as_of_date) if fundamentals else None,
        },
        "score_history": [
            {"value": s.score_value, "band": s.band, "ts": str(s.ts)}
            for s in scores
        ],
    }


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
