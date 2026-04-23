"""
app/api/v1/endpoints/scoring.py
GET  /scoring/leaderboard              — top-N equity by latest score
GET  /scoring/summary                  — band distribution + portfolio avg
GET  /scoring/needs-attention          — WATCH/REJECT + stale + at-risk holdings
POST /scoring/equity/{instrument_id}   — score one instrument (trader+)
POST /scoring/equity/batch             — score many / all equity (founder)
GET  /scoring/equity/{instrument_id}/history — score history
"""
import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from app.db.session import get_db
from app.core.deps import get_current_user, require_founder, require_trader_or_above
from app.models.models import (
    Instrument, FundamentalsSnapshot, Score, Holding,
    User, AssetType, CapBucket, ScoreBand,
)
from app.services.engines.equity_engine import FundamentalsInput, score_equity

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/scoring", tags=["Scoring"])

BAND_INTERPRETATION = {
    "STRONG_BUY": {"label": "Strong Buy", "color": "#10b981", "description": "Exceptional fundamentals. High conviction long position."},
    "BUY":        {"label": "Buy",        "color": "#4ade80", "description": "Solid fundamentals. Good entry point for accumulation."},
    "HOLD":       {"label": "Hold",       "color": "#fde047", "description": "Adequate fundamentals. Maintain position, monitor closely."},
    "WATCH":      {"label": "Watch",      "color": "#fb923c", "description": "Weak fundamentals. Caution advised — review before trading."},
    "REJECT":     {"label": "Reject",     "color": "#f87171", "description": "Poor fundamentals. Avoid or exit position."},
}


def _interpretation(band: str) -> dict:
    return BAND_INTERPRETATION.get(band, {"label": band, "color": "#9ca3af", "description": "Score pending."})


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class BatchScoreRequest(BaseModel):
    instrument_ids: Optional[list[int]] = None


# ── helpers ───────────────────────────────────────────────────────────────────

def _latest_fund(db: Session, instrument_id: int) -> FundamentalsSnapshot | None:
    return (
        db.query(FundamentalsSnapshot)
        .filter(FundamentalsSnapshot.instrument_id == instrument_id)
        .order_by(FundamentalsSnapshot.as_of_date.desc())
        .first()
    )


def _build_input(fund: FundamentalsSnapshot, inst: Instrument) -> FundamentalsInput:
    # extra_json holds growth %s; fund.sales/profit/eps hold absolute values for display only
    extra = fund.extra_json or {}
    cap = inst.cap_bucket.value if inst.cap_bucket else "LARGE"
    return FundamentalsInput(
        pe=fund.pe,
        roe=fund.roe,
        debt_equity=fund.debt_equity,
        eps_growth_pct=extra.get("eps_growth"),
        sales_growth_pct=extra.get("sales_growth"),
        profit_growth_pct=extra.get("profit_growth"),
        fii_pct=fund.fii_pct,
        dii_pct=fund.dii_pct,
        market_cap=fund.market_cap,
        fii_trend="NEUTRAL",
        management_score=5,
        macro_alignment=5,
        cap_bucket=cap,
    )


def _save_score(db: Session, instrument_id: int, result) -> Score:
    try:
        band_enum = ScoreBand(result.band)
    except ValueError:
        band_enum = ScoreBand.WATCH

    score = Score(
        instrument_id=instrument_id,
        strategy_id=None,
        score_value=result.total_score,
        band=band_enum,
        factors_json=result.factors,
    )
    db.add(score)
    db.commit()
    db.refresh(score)
    return score


# ── shared subquery helper ────────────────────────────────────────────────────

def _latest_score_subquery(db: Session):
    """Subquery returning the latest score id per instrument_id. Uses max(id) not max(ts)
    to avoid duplicate rows when batch scoring creates scores with identical timestamps."""
    id_sq = (
        db.query(Score.instrument_id, func.max(Score.id).label("max_id"))
        .group_by(Score.instrument_id)
        .subquery()
    )
    return id_sq


# ── GET /leaderboard ──────────────────────────────────────────────────────────

@router.get("/leaderboard")
def score_leaderboard(
    cap_bucket: Optional[str] = Query(None, description="LARGE | MID | SMALL"),
    limit: int = Query(10, ge=1, le=50),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    id_sq = _latest_score_subquery(db)

    q = (
        db.query(Score, Instrument)
        .join(id_sq, Score.id == id_sq.c.max_id)
        .join(Instrument, Score.instrument_id == Instrument.id)
        .filter(Instrument.asset_type == AssetType.EQUITY, Instrument.is_active == True)
    )

    if cap_bucket:
        try:
            q = q.filter(Instrument.cap_bucket == CapBucket(cap_bucket.upper()))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"Invalid cap_bucket: {cap_bucket}")

    rows = q.order_by(Score.score_value.desc()).limit(limit).all()

    total_scored = (
        db.query(func.count(func.distinct(Score.instrument_id)))
        .join(Instrument, Score.instrument_id == Instrument.id)
        .filter(Instrument.asset_type == AssetType.EQUITY, Instrument.is_active == True)
        .scalar()
    ) or 0

    total_instruments = (
        db.query(func.count(Instrument.id))
        .filter(Instrument.asset_type == AssetType.EQUITY, Instrument.is_active == True)
        .scalar()
    ) or 0

    return {
        "leaderboard": [
            {
                "rank":          idx + 1,
                "instrument_id": inst.id,
                "symbol":        inst.symbol,
                "exchange":      inst.exchange,
                "sector":        inst.sector,
                "cap_bucket":    inst.cap_bucket.value if inst.cap_bucket else None,
                "score":         score.score_value,
                "band":          score.band.value if score.band else None,
                "factors_count": len(score.factors_json or {}),
                "scored_at":     str(score.ts),
            }
            for idx, (score, inst) in enumerate(rows)
        ],
        "total_scored":      total_scored,
        "total_instruments": total_instruments,
    }


# ── GET /summary ──────────────────────────────────────────────────────────────

@router.get("/summary")
def scoring_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    all_instruments = (
        db.query(Instrument)
        .filter(Instrument.asset_type == AssetType.EQUITY, Instrument.is_active == True)
        .all()
    )
    inst_map = {i.id: i for i in all_instruments}
    all_ids = list(inst_map.keys())

    # Latest score per instrument
    id_sq = (
        db.query(Score.instrument_id, func.max(Score.id).label("max_id"))
        .filter(Score.instrument_id.in_(all_ids))
        .group_by(Score.instrument_id)
        .subquery()
    )
    latest_scores = (
        db.query(Score)
        .join(id_sq, Score.id == id_sq.c.max_id)
        .all()
    )
    scored_map = {s.instrument_id: s for s in latest_scores}

    # Band distribution
    by_band = {b.value: 0 for b in ScoreBand}
    by_band["NOT_SCORED"] = 0
    for inst_id in all_ids:
        s = scored_map.get(inst_id)
        if s and s.band:
            by_band[s.band.value] += 1
        else:
            by_band["NOT_SCORED"] += 1

    # Cap bucket aggregation
    by_cap: dict[str, dict] = {}
    for inst_id, inst in inst_map.items():
        bucket = inst.cap_bucket.value if inst.cap_bucket else "UNKNOWN"
        if bucket not in by_cap:
            by_cap[bucket] = {"scores": [], "count": 0}
        by_cap[bucket]["count"] += 1
        s = scored_map.get(inst_id)
        if s:
            by_cap[bucket]["scores"].append(s.score_value)

    by_cap_bucket = {
        bucket: {
            "avg_score": round(sum(v["scores"]) / len(v["scores"]), 1) if v["scores"] else None,
            "count":     v["count"],
        }
        for bucket, v in by_cap.items()
    }

    # Portfolio avg score
    holding_inst_ids = {
        row[0] for row in db.query(Holding.instrument_id).all()
    }
    portfolio_scores = [
        scored_map[iid].score_value
        for iid in holding_inst_ids
        if iid in scored_map
    ]
    portfolio_avg = round(sum(portfolio_scores) / len(portfolio_scores), 1) if portfolio_scores else None

    # Most recent score timestamp
    last_scored_at = (
        db.query(func.max(Score.ts)).scalar()
    )

    return {
        "by_band":            by_band,
        "by_cap_bucket":      by_cap_bucket,
        "portfolio_avg_score": portfolio_avg,
        "last_scored_at":     str(last_scored_at) if last_scored_at else None,
    }


# ── GET /needs-attention ──────────────────────────────────────────────────────

@router.get("/needs-attention")
def needs_attention(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    stale_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)

    all_instruments = (
        db.query(Instrument)
        .filter(Instrument.asset_type == AssetType.EQUITY, Instrument.is_active == True)
        .all()
    )
    all_ids = [i.id for i in all_instruments]

    # Latest score per instrument
    id_sq = (
        db.query(Score.instrument_id, func.max(Score.id).label("max_id"))
        .filter(Score.instrument_id.in_(all_ids))
        .group_by(Score.instrument_id)
        .subquery()
    )
    latest_scores = (
        db.query(Score)
        .join(id_sq, Score.id == id_sq.c.max_id)
        .all()
    )
    scored_map = {s.instrument_id: s for s in latest_scores}

    holding_inst_ids = {
        row[0] for row in db.query(Holding.instrument_id).all()
    }

    flagged = []
    for inst in all_instruments:
        s = scored_map.get(inst.id)
        in_portfolio = inst.id in holding_inst_ids
        score_val = s.score_value if s else None
        band_val = s.band.value if s and s.band else None

        reason = None

        # Highest priority: portfolio holding with score below 40
        if in_portfolio and score_val is not None and score_val < 40:
            reason = "Portfolio holding at risk"
        # Second: WATCH or REJECT band
        elif band_val in ("WATCH", "REJECT"):
            reason = "Low score"
        # Third: never scored or stale (> 30 days)
        elif s is None:
            reason = "Not scored recently"
        elif s.ts.replace(tzinfo=timezone.utc) < stale_cutoff:
            reason = "Not scored recently"

        if reason:
            flagged.append({
                "instrument_id": inst.id,
                "symbol":        inst.symbol,
                "cap_bucket":    inst.cap_bucket.value if inst.cap_bucket else None,
                "score":         score_val,
                "band":          band_val,
                "in_portfolio":  in_portfolio,
                "reason":        reason,
            })

    return {"count": len(flagged), "instruments": flagged}


# ── POST /equity/batch (must be before /{instrument_id} to avoid path conflict) ──

@router.post("/equity/batch")
def batch_score_equity(
    payload: BatchScoreRequest = BatchScoreRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    if payload.instrument_ids:
        instruments = (
            db.query(Instrument)
            .filter(
                Instrument.id.in_(payload.instrument_ids),
                Instrument.asset_type == AssetType.EQUITY,
                Instrument.is_active == True,
            )
            .all()
        )
    else:
        instruments = (
            db.query(Instrument)
            .filter(Instrument.asset_type == AssetType.EQUITY, Instrument.is_active == True)
            .all()
        )

    scored, skipped = 0, 0
    results = []

    for inst in instruments:
        fund = _latest_fund(db, inst.id)
        if not fund:
            logger.info("Skipping %s — no fundamentals", inst.symbol)
            skipped += 1
            continue

        try:
            fi = _build_input(fund, inst)
            result = score_equity(fi)
            _save_score(db, inst.id, result)
            scored += 1
            results.append({
                "id":             inst.id,
                "symbol":         inst.symbol,
                "score":          result.total_score,
                "band":           result.band,
                "interpretation": _interpretation(result.band),
            })
            logger.info("Scored %s → %.1f (%s)", inst.symbol, result.total_score, result.band)
        except Exception as exc:
            logger.error("Error scoring %s: %s", inst.symbol, exc)
            skipped += 1

    top_scorer = max(results, key=lambda r: r["score"]) if results else None
    needs_attention = [r for r in results if r["band"] in ("WATCH", "REJECT")]

    return {
        "scored":          scored,
        "skipped":         skipped,
        "results":         results,
        "top_scorer":      top_scorer,
        "needs_attention": needs_attention,
    }


# ── POST /equity/{instrument_id} ──────────────────────────────────────────────

@router.post("/equity/{instrument_id}")
def score_single_equity(
    instrument_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_trader_or_above),
):
    inst = db.query(Instrument).filter(
        Instrument.id == instrument_id,
        Instrument.is_active == True,
    ).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instrument not found")

    fund = _latest_fund(db, instrument_id)
    if not fund:
        raise HTTPException(
            status_code=400,
            detail="No fundamentals data found. Add fundamentals first.",
        )

    fi = _build_input(fund, inst)
    result = score_equity(fi)
    _save_score(db, instrument_id, result)

    return {
        "instrument_id":  instrument_id,
        "symbol":         inst.symbol,
        "cap_bucket":     inst.cap_bucket.value if inst.cap_bucket else None,
        "score":          result.total_score,
        "band":           result.band,
        "interpretation": _interpretation(result.band),
        "factors":        result.factors,
        "pass_count":     result.pass_count,
        "total_factors":  result.total_factors,
    }


# ── GET /equity/{instrument_id}/history ──────────────────────────────────────

@router.get("/equity/{instrument_id}/history")
def score_history(
    instrument_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    inst = db.query(Instrument).filter(Instrument.id == instrument_id).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instrument not found")

    scores = (
        db.query(Score)
        .filter(Score.instrument_id == instrument_id)
        .order_by(Score.ts.desc())
        .limit(20)
        .all()
    )

    return {
        "instrument_id": instrument_id,
        "symbol":        inst.symbol,
        "scores": [
            {
                "value": s.score_value,
                "band":  s.band.value if s.band else None,
                "ts":    str(s.ts),
            }
            for s in scores
        ],
    }
