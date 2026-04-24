"""
app/api/v1/endpoints/mcx.py
POST /mcx/signals/generate/{instrument_id}  — generate MCX signal for one commodity
POST /mcx/signals/generate/batch            — generate signals for all MCX instruments
GET  /mcx/signals                           — list MCX signals (filters: status, side)
GET  /mcx/dashboard                         — snapshot + signal for all 3 commodities
GET  /mcx/macro-events                      — list macro events (filter: commodity)
POST /mcx/macro-events                      — create macro event (FOUNDER only)
GET  /mcx/session-status                    — current MCX session (no auth)
GET  /mcx/contracts/{symbol}                — contract chain + rollover info
POST /mcx/contracts/{symbol}/set-expiry     — set contract expiry (FOUNDER only)
"""
import logging
from datetime import datetime, timezone, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional

from app.db.session import get_db
from app.core.deps import get_current_user, require_founder
from app.models.models import (
    Signal, Instrument, MarketSnapshot, MacroEvent, User,
    AssetType, SignalStatus, SignalSide,
)
from app.services.engines.mcx_engine import (
    score_mcx, get_current_mcx_session, build_mcx_input_from_db,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcx", tags=["MCX"])


# ── Pydantic request schemas ──────────────────────────────────────────────────

class MacroEventCreate(BaseModel):
    type: str                   # GEOPOLITICS | BUDGET | WEATHER | NEWS
    title: str
    sentiment: str              # POSITIVE | NEGATIVE | NEUTRAL
    commodity: Optional[str] = None  # CRUDEOIL | GOLD | SILVER | None = global


class SetExpiryRequest(BaseModel):
    expiry_date: str            # YYYY-MM-DD


# ── Helpers ───────────────────────────────────────────────────────────────────

def _latest_mcx_snapshot(db: Session, instrument_id: int) -> MarketSnapshot | None:
    return (
        db.query(MarketSnapshot)
        .filter(MarketSnapshot.instrument_id == instrument_id)
        .order_by(MarketSnapshot.ts.desc())
        .first()
    )


def _active_macro_events(
    db: Session,
    commodity: str | None,
    event_type: str | None = None,
) -> list[MacroEvent]:
    """Return macro events relevant to a commodity (or global events), optionally filtered by type."""
    q = db.query(MacroEvent)
    if commodity:
        q = q.filter(
            MacroEvent.tags_json["commodity"].as_string() == commodity
        )
    if event_type:
        q = q.filter(MacroEvent.type == event_type.upper())
    return q.order_by(MacroEvent.created_at.desc()).limit(20).all()


def _mcx_signal_to_dict(s: Signal, instrument: Instrument) -> dict:
    meta = s.reasons_json or {}
    if isinstance(meta, list):
        reasons = meta
        bull_score = bear_score = session = session_note = None
        expiry_warning = rollover_recommended = False
    else:
        reasons = meta.get("reasons", [])
        bull_score = meta.get("bull_score")
        bear_score = meta.get("bear_score")
        session = meta.get("session")
        session_note = meta.get("session_note", "")
        expiry_warning = meta.get("expiry_warning", False)
        rollover_recommended = meta.get("rollover_recommended", False)

    return {
        "id":                   s.id,
        "symbol":               instrument.symbol if instrument else "",
        "exchange":             instrument.exchange if instrument else "",
        "sector":               instrument.sector if instrument else None,
        "side":                 s.side.value if s.side else s.side,
        "confidence":           s.confidence,
        "target_pct":           s.target_pct,
        "stop_pct":             s.stop_pct,
        "status":               s.status.value if s.status else s.status,
        "reasons":              reasons,
        "bull_score":           bull_score,
        "bear_score":           bear_score,
        "session":              session,
        "session_note":         session_note,
        "expiry_warning":       expiry_warning,
        "rollover_recommended": rollover_recommended,
        "review_date":          str(s.review_date) if s.review_date else None,
        "created_at":           str(s.ts),
    }


def _save_mcx_signal(
    db: Session,
    instrument: Instrument,
    result,
    snapshot: MarketSnapshot,
) -> Signal:
    """Persist a MCXSignalResult to the signals table."""
    meta = {
        "reasons":              result.reasons,
        "bull_score":           result.bull_score,
        "bear_score":           result.bear_score,
        "session":              result.session,
        "session_note":         result.session_note,
        "expiry_warning":       result.expiry_warning,
        "rollover_recommended": result.rollover_recommended,
        "close_at_signal":      snapshot.close if snapshot else None,
        "rsi_at_signal":        snapshot.rsi if snapshot else None,
    }
    review_dt = datetime.now(tz=timezone.utc) + timedelta(days=2)
    sig = Signal(
        instrument_id=instrument.id,
        side=SignalSide(result.signal),
        confidence=result.confidence,
        target_pct=result.target_pct,
        stop_pct=result.stop_pct,
        status=SignalStatus.PENDING,
        reasons_json=meta,
        review_date=review_dt,
    )
    db.add(sig)
    db.commit()
    db.refresh(sig)
    return sig


def _get_mcx_instrument(db: Session, symbol: str) -> Instrument:
    """Lookup an active MCX instrument by symbol, raise 404 if not found."""
    inst = (
        db.query(Instrument)
        .filter(
            Instrument.symbol == symbol.upper(),
            Instrument.asset_type == AssetType.MCX,
            Instrument.is_active == True,
        )
        .first()
    )
    if not inst:
        raise HTTPException(status_code=404, detail=f"MCX instrument '{symbol.upper()}' not found")
    return inst


def _build_contract_info(inst: Instrument, snapshot: "MarketSnapshot | None") -> dict:
    """Compute contract chain, expiry countdown, and rollover recommendation."""
    today = datetime.now(tz=timezone.utc).date()

    if inst.expiry:
        expiry_dt   = inst.expiry
        expiry_date = expiry_dt.date() if hasattr(expiry_dt, "date") else expiry_dt
    else:
        expiry_date = today + timedelta(days=25)

    days_to_expiry = (expiry_date - today).days

    if days_to_expiry <= 5:
        recommendation = "EXIT"
    elif days_to_expiry <= 10:
        recommendation = "ROLLOVER"
    else:
        recommendation = "HOLD"

    if days_to_expiry <= 3:
        rollover_urgency = "IMMEDIATE"
    elif days_to_expiry <= 10:
        rollover_urgency = "SOON"
    else:
        rollover_urgency = "NOT_YET"

    next_expiry      = expiry_date + timedelta(days=30)
    next_days        = (next_expiry - today).days

    # Infer next-month flow from OI: high OI = institutional accumulation (BUYING)
    next_month_flow = "NEUTRAL"
    if snapshot and snapshot.oi is not None:
        if snapshot.oi > 100_000:
            next_month_flow = "BUYING"
        elif snapshot.oi < 20_000:
            next_month_flow = "SELLING"

    return {
        "symbol": inst.symbol,
        "current_month": {
            "expiry_date":    str(expiry_date),
            "days_to_expiry": days_to_expiry,
            "latest_price":   snapshot.close  if snapshot else None,
            "oi":             snapshot.oi     if snapshot else None,
            "volume":         snapshot.volume if snapshot else None,
            "recommendation": recommendation,
        },
        "next_month": {
            "expiry_date":    str(next_expiry),
            "days_to_expiry": next_days,
            "next_month_flow": next_month_flow,
        },
        "rollover_urgency": rollover_urgency,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/signals/generate/batch")
def generate_mcx_batch(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate MCX signals for all active MCX instruments."""
    instruments = (
        db.query(Instrument)
        .filter(Instrument.asset_type == AssetType.MCX, Instrument.is_active == True)
        .all()
    )
    if not instruments:
        raise HTTPException(status_code=404, detail="No active MCX instruments found")

    generated = []
    skipped = []
    no_trade = []

    for inst in instruments:
        # Idempotency: skip if PENDING signal already exists today
        today_dt = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        existing = (
            db.query(Signal)
            .filter(
                Signal.instrument_id == inst.id,
                Signal.status == SignalStatus.PENDING,
                Signal.ts >= today_dt,
            )
            .first()
        )
        if existing:
            skipped.append(inst.symbol)
            continue

        snapshot = _latest_mcx_snapshot(db, inst.id)
        if not snapshot:
            skipped.append(inst.symbol)
            continue

        macro_events = _active_macro_events(db, inst.symbol)
        mcx_input = build_mcx_input_from_db(inst, snapshot, macro_events)
        session, session_note = get_current_mcx_session()
        mcx_input.trading_session = session

        result = score_mcx(mcx_input)

        if result.signal == "NO_TRADE":
            no_trade.append({
                "symbol": inst.symbol,
                "bull_score": result.bull_score,
                "bear_score": result.bear_score,
                "reasons": result.reasons,
            })
            continue

        sig = _save_mcx_signal(db, inst, result, snapshot)
        generated.append(_mcx_signal_to_dict(sig, inst))

    return {
        "generated": generated,
        "skipped": skipped,
        "no_trade": no_trade,
        "total_instruments": len(instruments),
    }


@router.post("/signals/generate/{instrument_id}")
def generate_mcx_signal(
    instrument_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Generate MCX signal for a single commodity instrument."""
    inst = db.query(Instrument).filter(
        Instrument.id == instrument_id,
        Instrument.asset_type == AssetType.MCX,
    ).first()
    if not inst:
        raise HTTPException(status_code=404, detail="MCX instrument not found")

    # Idempotency: one PENDING signal per instrument per day
    today_dt = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    existing = (
        db.query(Signal)
        .filter(
            Signal.instrument_id == inst.id,
            Signal.status == SignalStatus.PENDING,
            Signal.ts >= today_dt,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"PENDING signal already exists for {inst.symbol} today (id={existing.id})",
        )

    snapshot = _latest_mcx_snapshot(db, inst.id)
    if not snapshot:
        raise HTTPException(status_code=400, detail=f"No market snapshot found for {inst.symbol}")

    macro_events = _active_macro_events(db, inst.symbol)
    mcx_input = build_mcx_input_from_db(inst, snapshot, macro_events)
    session, session_note = get_current_mcx_session()
    mcx_input.trading_session = session

    result = score_mcx(mcx_input)

    if result.signal == "NO_TRADE":
        return {
            "signal": "NO_TRADE",
            "symbol": inst.symbol,
            "bull_score": result.bull_score,
            "bear_score": result.bear_score,
            "session": result.session,
            "session_note": result.session_note,
            "reasons": result.reasons,
            "message": "Signal not strong enough to trade — bull/bear diff < 20 points",
        }

    sig = _save_mcx_signal(db, inst, result, snapshot)
    return _mcx_signal_to_dict(sig, inst)


@router.get("/signals")
def get_mcx_signals(
    status: Optional[str] = Query(None, description="PENDING|APPROVED|REJECTED|all"),
    side: Optional[str] = Query(None, description="BUY|SELL"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List MCX signals with optional filters."""
    mcx_ids = [
        r[0] for r in db.query(Instrument.id)
        .filter(Instrument.asset_type == AssetType.MCX, Instrument.is_active == True)
        .all()
    ]
    if not mcx_ids:
        return {"signals": [], "total": 0}

    q = db.query(Signal).filter(Signal.instrument_id.in_(mcx_ids))

    if status and status != "all":
        try:
            q = q.filter(Signal.status == SignalStatus(status.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
    elif not status:
        q = q.filter(Signal.status.in_([SignalStatus.PENDING, SignalStatus.APPROVED]))

    if side:
        try:
            q = q.filter(Signal.side == SignalSide(side.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid side: {side}")

    signals = q.order_by(Signal.ts.desc()).limit(50).all()

    inst_map = {i.id: i for i in db.query(Instrument).filter(Instrument.id.in_(mcx_ids)).all()}
    result = [_mcx_signal_to_dict(s, inst_map.get(s.instrument_id)) for s in signals]
    return {"signals": result, "total": len(result)}


@router.get("/dashboard")
def get_mcx_dashboard(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    MCX dashboard: latest snapshot + active signal for each commodity.
    Returns one card per instrument with price, RSI, OI, bull/bear score, session info.
    """
    instruments = (
        db.query(Instrument)
        .filter(Instrument.asset_type == AssetType.MCX, Instrument.is_active == True)
        .all()
    )

    today_dt = datetime.now(tz=timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    session, session_note = get_current_mcx_session()

    cards = []
    for inst in instruments:
        snapshot = _latest_mcx_snapshot(db, inst.id)

        # Latest active signal today
        active_signal = (
            db.query(Signal)
            .filter(
                Signal.instrument_id == inst.id,
                Signal.status.in_([SignalStatus.PENDING, SignalStatus.APPROVED]),
                Signal.ts >= today_dt,
            )
            .order_by(Signal.ts.desc())
            .first()
        )

        snap_data = None
        if snapshot:
            snap_data = {
                "close":  snapshot.close,
                "open":   snapshot.open,
                "high":   snapshot.high,
                "low":    snapshot.low,
                "volume": snapshot.volume,
                "oi":     snapshot.oi,
                "rsi":    snapshot.rsi,
                "sma_20": snapshot.sma_20,
                "sma_50": snapshot.sma_50,
                "ts":     str(snapshot.ts),
            }

        card = {
            "instrument_id": inst.id,
            "symbol":        inst.symbol,
            "exchange":      inst.exchange,
            "sector":        inst.sector,
            "snapshot":      snap_data,
            "signal":        _mcx_signal_to_dict(active_signal, inst) if active_signal else None,
            "session":       session,
            "session_note":  session_note,
        }
        cards.append(card)

    return {
        "cards":        cards,
        "session":      session,
        "session_note": session_note,
        "generated_at": str(datetime.now(tz=timezone.utc)),
    }


@router.get("/macro-events")
def get_macro_events(
    commodity: Optional[str] = Query(None, description="CRUDEOIL|GOLD|SILVER"),
    type: Optional[str] = Query(None, description="GEOPOLITICS|BUDGET|WEATHER|NEWS"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """List macro events. Filter by commodity tag and/or event type."""
    events = _active_macro_events(db, commodity, event_type=type)
    return {
        "events": [
            {
                "id":             e.id,
                "type":           e.type,
                "title":          e.title,
                "sentiment":      e.sentiment,
                "commodity":      (e.tags_json or {}).get("commodity"),
                "effective_from": str(e.effective_from) if e.effective_from else None,
                "created_at":     str(e.created_at),
            }
            for e in events
        ],
        "total": len(events),
    }


@router.post("/macro-events")
def create_macro_event(
    body: MacroEventCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    """Create a new macro event. FOUNDER only."""
    valid_types = {"GEOPOLITICS", "BUDGET", "WEATHER", "NEWS"}
    valid_sentiments = {"POSITIVE", "NEGATIVE", "NEUTRAL"}

    if body.type.upper() not in valid_types:
        raise HTTPException(status_code=400, detail=f"type must be one of {valid_types}")
    if body.sentiment.upper() not in valid_sentiments:
        raise HTTPException(status_code=400, detail=f"sentiment must be one of {valid_sentiments}")

    event = MacroEvent(
        type=body.type.upper(),
        title=body.title,
        sentiment=body.sentiment.upper(),
        effective_from=datetime.now(tz=timezone.utc),
        tags_json={"commodity": body.commodity} if body.commodity else {},
    )
    db.add(event)
    db.commit()
    db.refresh(event)

    return {
        "id":             event.id,
        "type":           event.type,
        "title":          event.title,
        "sentiment":      event.sentiment,
        "commodity":      (event.tags_json or {}).get("commodity"),
        "effective_from": str(event.effective_from),
        "created_at":     str(event.created_at),
    }


@router.get("/session-status")
def get_session_status():
    """
    Current MCX session status — no auth required.
    MCX trading hours: weekdays 09:00–23:30 IST, Saturday 09:00–14:00 IST, Sunday closed.
    Sessions: MORNING 09:00–13:00, AFTERNOON 13:00–17:00, EVENING 17:00–23:30, OFF_HOURS otherwise.
    """
    now_utc = datetime.now(tz=timezone.utc)
    # Derive IST without pytz by offsetting UTC+5:30
    ist          = now_utc + timedelta(hours=5, minutes=30)
    ist_total    = ist.hour * 60 + ist.minute
    weekday      = ist.weekday()            # 0=Mon … 5=Sat, 6=Sun

    MORNING_OPEN    = 9  * 60        # 09:00
    AFTERNOON_START = 13 * 60        # 13:00
    EVENING_START   = 17 * 60        # 17:00
    WEEKDAY_CLOSE   = 23 * 60 + 30  # 23:30
    SATURDAY_CLOSE  = 14 * 60        # 14:00

    current_session = "OFF_HOURS"
    session_note    = ""
    market_open     = False
    next_session    = ""

    if weekday == 6:                 # Sunday — fully closed
        current_session = "OFF_HOURS"
        session_note    = "MCX closed on Sundays"
        market_open     = False
        next_session    = "Monday morning session opens at 09:00 IST"

    elif weekday == 5:               # Saturday — 09:00–14:00 only
        if MORNING_OPEN <= ist_total < AFTERNOON_START:
            current_session = "MORNING"
            session_note    = "MCX Saturday morning session (09:00–13:00 IST)"
            market_open     = True
            next_session    = "Afternoon session at 13:00 IST (closes 14:00)"
        elif AFTERNOON_START <= ist_total < SATURDAY_CLOSE:
            current_session = "AFTERNOON"
            session_note    = "MCX Saturday afternoon session (13:00–14:00 IST)"
            market_open     = True
            next_session    = "Closes at 14:00 IST — reopens Monday 09:00 IST"
        else:
            current_session = "OFF_HOURS"
            session_note    = "MCX closed — Saturday trading ends at 14:00 IST"
            market_open     = False
            next_session    = "Monday morning session opens at 09:00 IST"

    else:                            # Monday–Friday — full day 09:00–23:30
        if MORNING_OPEN <= ist_total < AFTERNOON_START:
            current_session = "MORNING"
            session_note    = "MCX morning session (09:00–13:00 IST)"
            market_open     = True
            next_session    = "Afternoon session at 13:00 IST"
        elif AFTERNOON_START <= ist_total < EVENING_START:
            current_session = "AFTERNOON"
            session_note    = "MCX afternoon session (13:00–17:00 IST)"
            market_open     = True
            next_session    = "Evening session at 17:00 IST"
        elif EVENING_START <= ist_total < WEEKDAY_CLOSE:
            current_session = "EVENING"
            session_note    = "MCX evening session (17:00–23:30 IST)"
            market_open     = True
            next_session    = "Closes at 23:30 IST — reopens next trading day at 09:00 IST"
        else:
            current_session = "OFF_HOURS"
            market_open     = False
            if ist_total < MORNING_OPEN:
                session_note = "MCX pre-market — off hours"
                next_session = "Morning session opens at 09:00 IST today"
            else:
                # After 23:30
                session_note = "MCX closed for the day"
                if weekday == 4:    # Friday night → Saturday
                    next_session = "Saturday morning session opens at 09:00 IST"
                else:
                    next_session = "Morning session opens at 09:00 IST tomorrow"

    return {
        "current_session": current_session,
        "session_note":    session_note,
        "market_open":     market_open,
        "next_session":    next_session,
        "ist_time":        ist.strftime("%Y-%m-%d %H:%M:%S IST"),
    }


@router.get("/contracts/{symbol}")
def get_contract_info(
    symbol: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Contract chain for a commodity: current month expiry, days to expiry,
    rollover recommendation, and next-month forward flow.
    Falls back to today+25 days if no expiry date is set on the instrument.
    """
    inst     = _get_mcx_instrument(db, symbol)
    snapshot = _latest_mcx_snapshot(db, inst.id)
    return _build_contract_info(inst, snapshot)


@router.post("/contracts/{symbol}/set-expiry")
def set_contract_expiry(
    symbol: str,
    body: SetExpiryRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    """Set the contract expiry date for an MCX instrument. FOUNDER only."""
    inst = _get_mcx_instrument(db, symbol)

    try:
        expiry_dt = datetime.strptime(body.expiry_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise HTTPException(status_code=400, detail="expiry_date must be in YYYY-MM-DD format")

    today = datetime.now(tz=timezone.utc).date()
    if expiry_dt.date() <= today:
        raise HTTPException(status_code=400, detail="expiry_date must be a future date")

    inst.expiry = expiry_dt
    db.commit()
    db.refresh(inst)

    snapshot = _latest_mcx_snapshot(db, inst.id)
    return {
        "message":   f"Expiry for {inst.symbol} updated to {body.expiry_date}",
        "instrument_id": inst.id,
        "symbol":    inst.symbol,
        "exchange":  inst.exchange,
        "expiry":    str(inst.expiry.date()),
        **_build_contract_info(inst, snapshot),
    }
