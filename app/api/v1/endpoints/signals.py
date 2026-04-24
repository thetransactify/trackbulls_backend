"""
app/api/v1/endpoints/signals.py
GET  /signals/equity              — equity signals (filters: side, status, strength, min_confidence)
GET  /signals/mcx                 — MCX commodity signals
GET  /signals/stats               — signal counts by status/side
GET  /signals/{id}                — full signal detail + related orders
POST /signals/{id}/approve        — approve signal
POST /signals/{id}/reject         — reject signal
POST /signals/generate/equity     — generate signal for one instrument
POST /signals/generate/batch      — generate signals for all/selected instruments

Extra signal metadata (strength, hold_days, invalidation, score) is packed into
reasons_json alongside the reasons list because the Signal model has no dedicated
columns for those fields.
"""
import logging
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from app.db.session import get_db
from app.core.deps import get_current_user, require_founder, require_trader_or_above
from app.models.models import (
    Signal, Instrument, Score, Order, User,
    AssetType, SignalStatus, SignalSide,
)
from app.services.engines.signal_engine import SignalInput, generate_equity_signal

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/signals", tags=["Signals"])


# ── Pydantic request schemas ──────────────────────────────────────────────────

class GenerateSignalRequest(BaseModel):
    instrument_id: int
    rsi: Optional[float] = None
    sma_20: Optional[float] = None
    current_price: Optional[float] = None
    volume_trend: str = "NEUTRAL"
    momentum: str = "NEUTRAL"
    news_sentiment: str = "NEUTRAL"
    macro_sentiment: str = "NEUTRAL"


class BatchGenerateRequest(BaseModel):
    instrument_ids: Optional[list[int]] = None


# ── Serialiser ────────────────────────────────────────────────────────────────

def _signal_to_dict(s: Signal, instrument: Instrument, approved_user: User | None = None) -> dict:
    """Serialize a Signal row. Extra metadata is unpacked from reasons_json."""
    meta = s.reasons_json or {}
    # reasons_json may be a plain list (legacy) or the new envelope dict
    if isinstance(meta, list):
        reasons = meta
        strength = None
        hold_days = None
        invalidation = None
        score_at_signal = None
    else:
        reasons = meta.get("reasons", [])
        strength = meta.get("strength")
        hold_days = meta.get("hold_days")
        invalidation = meta.get("invalidation")
        score_at_signal = meta.get("score")

    return {
        "id":               s.id,
        "symbol":           instrument.symbol    if instrument else "",
        "exchange":         instrument.exchange  if instrument else "",
        "sector":           instrument.sector    if instrument else None,
        "asset_type":       instrument.asset_type.value if instrument and instrument.asset_type else "",
        "cap_bucket":       instrument.cap_bucket.value if instrument and instrument.cap_bucket else None,
        "side":             s.side.value if s.side else s.side,
        "confidence":       s.confidence,
        "strength":         strength,
        "target_pct":       s.target_pct,
        "stop_pct":         s.stop_pct,
        "status":           s.status.value if s.status else s.status,
        "reasons":          reasons,
        "invalidation":     invalidation,
        "hold_days":        hold_days,
        "score_at_signal":  score_at_signal,
        "review_date":      str(s.review_date) if s.review_date else None,
        "created_at":       str(s.ts),
        "approved_by_name": approved_user.name if approved_user else None,
    }


def _latest_score(db: Session, instrument_id: int) -> Score | None:
    """Fetch the most recent Score row for an instrument."""
    id_sq = (
        db.query(Score.instrument_id, func.max(Score.id).label("max_id"))
        .filter(Score.instrument_id == instrument_id)
        .group_by(Score.instrument_id)
        .subquery()
    )
    return db.query(Score).join(id_sq, Score.id == id_sq.c.max_id).first()


def _save_signal(db: Session, inst: Instrument, result, score_value: float) -> Signal:
    """Persist a SignalOutput to the signals table."""
    meta = {
        "reasons":      result.reasons,
        "strength":     result.strength,
        "hold_days":    result.hold_days,
        "invalidation": result.invalidation,
        "score":        score_value,
    }
    sig = Signal(
        instrument_id=inst.id,
        side=SignalSide(result.side),
        confidence=result.confidence,
        target_pct=result.target_pct,
        stop_pct=result.stop_pct,
        status=SignalStatus.PENDING,
        reasons_json=meta,
        review_date=result.review_date,
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    return sig


# ── GET /equity ───────────────────────────────────────────────────────────────

@router.get("/equity")
def get_equity_signals(
    side: Optional[str] = Query(None, description="BUY | SELL | HOLD"),
    status: Optional[str] = Query(None, description="all | pending | approved | rejected | executed"),
    strength: Optional[str] = Query(None, description="STRONG | MODERATE | WEAK"),
    min_confidence: Optional[float] = Query(None, ge=0, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = (
        db.query(Signal)
        .join(Instrument)
        .filter(Instrument.asset_type == AssetType.EQUITY)
    )

    if side:
        query = query.filter(Signal.side == side.upper())

    # Default: only PENDING and APPROVED; "all" bypasses the filter
    if status and status.lower() == "all":
        pass
    elif status:
        query = query.filter(Signal.status == status.upper())
    else:
        query = query.filter(Signal.status.in_([SignalStatus.PENDING, SignalStatus.APPROVED]))

    if min_confidence is not None:
        query = query.filter(Signal.confidence >= min_confidence)

    signals = query.order_by(Signal.ts.desc()).limit(100).all()

    result = []
    for s in signals:
        instrument = db.query(Instrument).filter(Instrument.id == s.instrument_id).first()
        approved_user = (
            db.query(User).filter(User.id == s.approved_by).first()
            if s.approved_by else None
        )
        d = _signal_to_dict(s, instrument, approved_user)

        # In-Python strength filter (stored inside JSON, not an indexed column)
        if strength and d.get("strength") != strength.upper():
            continue

        result.append(d)

    return {"count": len(result), "signals": result}


# ── GET /mcx ──────────────────────────────────────────────────────────────────

@router.get("/mcx")
def get_mcx_signals(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    signals = (
        db.query(Signal)
        .join(Instrument)
        .filter(Instrument.asset_type == AssetType.MCX)
        .order_by(Signal.ts.desc())
        .limit(20)
        .all()
    )
    result = []
    for s in signals:
        instrument = db.query(Instrument).filter(Instrument.id == s.instrument_id).first()
        result.append(_signal_to_dict(s, instrument))
    return {"count": len(result), "signals": result}


# ── GET /stats ────────────────────────────────────────────────────────────────

@router.get("/stats")
def signal_stats(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    all_signals = (
        db.query(Signal)
        .join(Instrument)
        .filter(Instrument.asset_type == AssetType.EQUITY)
        .all()
    )

    total_pending = total_approved = total_executed = total_rejected = 0
    strong_signals = bull_signals = bear_signals = 0
    confidence_values: list[float] = []

    for s in all_signals:
        status_val = s.status.value if s.status else ""
        side_val = s.side.value if s.side else ""

        if status_val == "PENDING":
            total_pending += 1
        elif status_val == "APPROVED":
            total_approved += 1
        elif status_val == "EXECUTED":
            total_executed += 1
        elif status_val == "REJECTED":
            total_rejected += 1

        if s.confidence is not None:
            confidence_values.append(s.confidence)
            if s.confidence >= 75:
                strong_signals += 1

        if side_val == "BUY" and status_val == "PENDING":
            bull_signals += 1
        elif side_val == "SELL" and status_val == "PENDING":
            bear_signals += 1

    avg_confidence = (
        round(sum(confidence_values) / len(confidence_values), 1)
        if confidence_values else None
    )

    return {
        "total_pending":   total_pending,
        "total_approved":  total_approved,
        "total_executed":  total_executed,
        "total_rejected":  total_rejected,
        "strong_signals":  strong_signals,
        "bull_signals":    bull_signals,
        "bear_signals":    bear_signals,
        "avg_confidence":  avg_confidence,
    }


# ── POST /generate/equity ─────────────────────────────────────────────────────

@router.post("/generate/equity")
def generate_single_signal(
    payload: GenerateSignalRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_trader_or_above),
):
    inst = db.query(Instrument).filter(
        Instrument.id == payload.instrument_id,
        Instrument.is_active == True,
    ).first()
    if not inst:
        raise HTTPException(status_code=404, detail="Instrument not found")

    score_row = _latest_score(db, payload.instrument_id)
    if not score_row:
        raise HTTPException(
            status_code=400,
            detail="Instrument not scored yet. Run scoring first.",
        )

    # Idempotency: return existing PENDING signal if one already exists
    existing = db.query(Signal).filter(
        Signal.instrument_id == payload.instrument_id,
        Signal.status == SignalStatus.PENDING,
    ).first()
    if existing:
        instrument = db.query(Instrument).filter(Instrument.id == existing.instrument_id).first()
        return {
            "message": "Existing pending signal returned",
            "signal": _signal_to_dict(existing, instrument),
        }

    cap = inst.cap_bucket.value if inst.cap_bucket else "LARGE"
    band = score_row.band.value if score_row.band else "HOLD"

    signal_input = SignalInput(
        instrument_id=inst.id,
        symbol=inst.symbol,
        score=score_row.score_value,
        band=band,
        rsi=payload.rsi,
        sma_20=payload.sma_20,
        current_price=payload.current_price,
        volume_trend=payload.volume_trend,
        momentum=payload.momentum,
        news_sentiment=payload.news_sentiment,
        macro_sentiment=payload.macro_sentiment,
        cap_bucket=cap,
    )

    result = generate_equity_signal(signal_input)

    if result.side == "NO_TRADE":
        return {
            "message": "No trade signal generated",
            "side":    "NO_TRADE",
            "score":   score_row.score_value,
            "band":    band,
            "reasons": result.reasons,
        }

    sig = _save_signal(db, inst, result, score_row.score_value)
    logger.info(
        "Generated %s signal for %s — confidence=%.1f strength=%s",
        result.side, inst.symbol, result.confidence, result.strength,
    )
    return {
        "message": "Signal generated",
        "signal":  _signal_to_dict(sig, inst),
    }


# ── POST /generate/batch ──────────────────────────────────────────────────────

@router.post("/generate/batch")
def generate_batch_signals(
    payload: BatchGenerateRequest = BatchGenerateRequest(),
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
            .filter(
                Instrument.asset_type == AssetType.EQUITY,
                Instrument.is_active == True,
            )
            .all()
        )

    generated = 0
    skipped_no_score = 0
    skipped_no_trade = 0
    signals_out = []

    # Build a map of instrument_id → latest score in one pass
    all_ids = [i.id for i in instruments]
    id_sq = (
        db.query(Score.instrument_id, func.max(Score.id).label("max_id"))
        .filter(Score.instrument_id.in_(all_ids))
        .group_by(Score.instrument_id)
        .subquery()
    )
    latest_scores = db.query(Score).join(id_sq, Score.id == id_sq.c.max_id).all()
    score_map = {s.instrument_id: s for s in latest_scores}

    # Existing PENDING signals (skip re-generation)
    pending_ids = {
        row[0]
        for row in db.query(Signal.instrument_id)
        .filter(
            Signal.instrument_id.in_(all_ids),
            Signal.status == SignalStatus.PENDING,
        )
        .all()
    }

    for inst in instruments:
        score_row = score_map.get(inst.id)
        if not score_row:
            skipped_no_score += 1
            continue

        if inst.id in pending_ids:
            logger.debug("Skipping %s — PENDING signal already exists", inst.symbol)
            skipped_no_trade += 1
            continue

        cap = inst.cap_bucket.value if inst.cap_bucket else "LARGE"
        band = score_row.band.value if score_row.band else "HOLD"

        signal_input = SignalInput(
            instrument_id=inst.id,
            symbol=inst.symbol,
            score=score_row.score_value,
            band=band,
            cap_bucket=cap,
        )

        try:
            result = generate_equity_signal(signal_input)
        except Exception as exc:
            logger.error("Signal generation error for %s: %s", inst.symbol, exc)
            skipped_no_trade += 1
            continue

        if result.side == "NO_TRADE":
            skipped_no_trade += 1
            continue

        sig = _save_signal(db, inst, result, score_row.score_value)
        generated += 1
        signals_out.append(_signal_to_dict(sig, inst))
        logger.info(
            "Batch: %s signal for %s — confidence=%.1f",
            result.side, inst.symbol, result.confidence,
        )

    return {
        "generated":          generated,
        "skipped_no_score":   skipped_no_score,
        "skipped_no_trade":   skipped_no_trade,
        "signals":            signals_out,
    }


# ── GET /{signal_id} ──────────────────────────────────────────────────────────

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
    approved_user = (
        db.query(User).filter(User.id == s.approved_by).first()
        if s.approved_by else None
    )

    detail = _signal_to_dict(s, instrument, approved_user)

    # score_at_signal_time — stored in reasons_json envelope; if missing fall back
    # to querying the score table around the signal creation time
    if detail.get("score_at_signal") is None:
        fallback_score = _latest_score(db, s.instrument_id)
        detail["score_at_signal"] = fallback_score.score_value if fallback_score else None

    # related_orders
    orders = db.query(Order).filter(Order.signal_id == signal_id).all()
    detail["related_orders"] = [
        {
            "id":       o.id,
            "side":     o.side.value if o.side else o.side,
            "quantity": o.quantity,
            "price":    o.price,
            "status":   o.status.value if o.status else o.status,
            "mode":     o.mode.value if o.mode else o.mode,
            "created_at": str(o.created_at),
        }
        for o in orders
    ]

    return detail


# ── POST /{signal_id}/approve ─────────────────────────────────────────────────

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
        raise HTTPException(status_code=400, detail=f"Signal is already {s.status.value}")

    s.status = SignalStatus.APPROVED
    s.approved_by = current_user.id
    db.commit()
    return {"message": "Signal approved", "signal_id": signal_id}


# ── POST /{signal_id}/reject ──────────────────────────────────────────────────

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
