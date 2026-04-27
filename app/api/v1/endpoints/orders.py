"""
app/api/v1/endpoints/orders.py
POST /orders                           — create paper/live order (with full validation)
GET  /orders                           — filterable order list
GET  /orders/summary                   — paper-trading P&L stats
GET  /orders/blotter                   — paginated blotter with filters
GET  /orders/pnl-summary               — period-based P&L summary
GET  /orders/open-positions            — currently open orders (CREATED/SENT)
POST /orders/bulk-cancel               — FOUNDER — cancel many open orders at once
GET  /orders/by-instrument/{id}        — order history for one instrument
GET  /orders/{id}                      — order detail
POST /orders/{id}/cancel               — cancel a single pending order
POST /orders/{id}/notes                — update order notes
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date, timezone, timedelta
import json

from app.db.session import get_db
from app.core.deps import get_current_user, require_trader_or_above, require_founder
from app.core.config import settings
from app.models.models import (
    Order, Signal, Instrument, Holding, AuditLog, User,
    OrderMode, OrderStatus, SignalStatus, SignalSide, AssetType, CapBucket,
)
from app.services.engines.order_engine import (
    OrderIntent, validate_order, generate_order_uid, calculate_order_pnl,
)

router = APIRouter(prefix="/orders", tags=["Orders"])


# ── Request schemas ───────────────────────────────────────────────────────────

class OrderCreate(BaseModel):
    instrument_id: Optional[int] = None
    symbol: Optional[str] = None
    side: SignalSide
    quantity: float
    price: float
    mode: OrderMode = OrderMode.PAPER
    broker: str = "ZERODHA"
    signal_id: Optional[int] = None
    notes: Optional[str] = ""


class NotesUpdate(BaseModel):
    notes: str


class BulkCancelRequest(BaseModel):
    order_ids: Optional[list[int]] = None
    cancel_all_open: bool = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def _today_start() -> datetime:
    # Naive datetime — matches the way SQLite stores Order.created_at
    return datetime.combine(date.today(), datetime.min.time())


def _enum_val(v) -> str:
    return v.value if hasattr(v, "value") else str(v)


def _payload_dict(raw_payload_json) -> dict:
    if isinstance(raw_payload_json, dict):
        return raw_payload_json
    if isinstance(raw_payload_json, str):
        try:
            parsed = json.loads(raw_payload_json)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}
    return {}


def _existing_positions_today(db: Session, instrument_id: int) -> list[dict]:
    """
    Order has no `instrument_id` column — link via signal.instrument_id, or fall back
    to scanning today's FILLED orders that share the same signal's instrument.
    """
    orders_today = (
        db.query(Order)
        .filter(
            Order.status == OrderStatus.FILLED,
            Order.created_at >= _today_start(),
        )
        .all()
    )
    positions = []
    for o in orders_today:
        sig_inst_id = None
        if o.signal_id:
            sig = db.query(Signal).filter(Signal.id == o.signal_id).first()
            sig_inst_id = sig.instrument_id if sig else None
        # raw_payload_json may also carry instrument_id from the create call
        payload_inst = _payload_dict(o.raw_payload_json).get("instrument_id")
        resolved_inst = sig_inst_id or payload_inst
        if resolved_inst == instrument_id:
            positions.append({
                "instrument_id": resolved_inst,
                "side":          _enum_val(o.side),
                "status":        _enum_val(o.status),
            })
    return positions


def _calc_order_pnl(o: Order, holding: Optional[Holding]) -> Optional[float]:
    """
    Per-order P&L using `(filled_price - avg_cost) * filled_qty` against the
    holding's avg_cost. In this paper-trading system both BUYs and SELLs are
    long-position fills (BUY=open/add, SELL=close/reduce), so we always pass
    side="BUY" to the engine helper — the formula `(exit - entry) * qty` is
    correct for long entries (≈0 right after a BUY) and long exits (realized
    gain after a SELL).
    """
    if o.status != OrderStatus.FILLED or not o.filled_price or not o.filled_qty:
        return None
    if not holding or not holding.avg_cost:
        return None
    pnl = calculate_order_pnl(
        side="BUY",
        quantity=o.filled_qty,
        entry_price=holding.avg_cost,
        exit_price=o.filled_price,
    )
    return pnl["gross_pnl"]


def _daily_loss_so_far(db: Session) -> float:
    """Sum the absolute value of negative P&L from today's filled orders."""
    today_orders = (
        db.query(Order)
        .filter(
            Order.status == OrderStatus.FILLED,
            Order.created_at >= _today_start(),
        )
        .all()
    )
    total_loss = 0.0
    for o in today_orders:
        sig = db.query(Signal).filter(Signal.id == o.signal_id).first() if o.signal_id else None
        inst_id = sig.instrument_id if sig else _payload_dict(o.raw_payload_json).get("instrument_id")
        if not inst_id:
            continue
        holding = db.query(Holding).filter(Holding.instrument_id == inst_id).first()
        pnl = _calc_order_pnl(o, holding)
        if pnl is not None and pnl < 0:
            total_loss += abs(pnl)
    return total_loss


def _update_holdings(db: Session, instrument: Instrument, side: str, qty: float, price: float):
    """Apply an order fill to the holdings table."""
    side_norm = side.upper() if isinstance(side, str) else _enum_val(side)
    holding = db.query(Holding).filter(Holding.instrument_id == instrument.id).first()

    if side_norm == "BUY":
        if holding:
            old_qty   = holding.quantity or 0
            old_cost  = holding.avg_cost or 0
            total_qty = old_qty + qty
            holding.avg_cost      = ((old_qty * old_cost) + (qty * price)) / total_qty if total_qty > 0 else price
            holding.quantity      = total_qty
            holding.thesis_status = "ACTIVE"
        else:
            holding = Holding(
                instrument_id=instrument.id,
                quantity=qty,
                avg_cost=price,
                asset_bucket=instrument.cap_bucket,
                thesis_status="ACTIVE",
            )
            db.add(holding)

    elif side_norm == "SELL":
        if not holding:
            raise HTTPException(status_code=400, detail=f"No holding to sell for {instrument.symbol}")
        new_qty = (holding.quantity or 0) - qty
        holding.quantity = new_qty
        if new_qty <= 0:
            holding.thesis_status = "EXITED"


def _audit(db: Session, user_id: int, action: str, entity_type: str, entity_id: int, after: dict):
    db.add(AuditLog(
        actor_user_id=user_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        after_json=after,
    ))


def _instrument_for_order(db: Session, o: Order) -> Optional[Instrument]:
    """Resolve instrument via signal or stored raw_payload."""
    if o.signal_id:
        sig = db.query(Signal).filter(Signal.id == o.signal_id).first()
        if sig:
            return db.query(Instrument).filter(Instrument.id == sig.instrument_id).first()
    inst_id = _payload_dict(o.raw_payload_json).get("instrument_id")
    if inst_id:
        return db.query(Instrument).filter(Instrument.id == inst_id).first()
    return None


def _serialize_order(o: Order, db: Session, with_pnl: bool = True) -> dict:
    inst = _instrument_for_order(db, o)
    holding = (
        db.query(Holding).filter(Holding.instrument_id == inst.id).first()
        if inst else None
    )
    sig = db.query(Signal).filter(Signal.id == o.signal_id).first() if o.signal_id else None
    payload = _payload_dict(o.raw_payload_json)

    return {
        "id":             o.id,
        "uid":            o.uid,
        "signal_id":      o.signal_id,
        "instrument_id":  inst.id if inst else payload.get("instrument_id"),
        "symbol":         inst.symbol if inst else None,
        "exchange":       inst.exchange if inst else None,
        "sector":         inst.sector if inst else None,
        "side":           _enum_val(o.side),
        "quantity":       o.quantity,
        "price":          o.price,
        "filled_qty":     o.filled_qty,
        "filled_price":   o.filled_price,
        "mode":           _enum_val(o.mode),
        "status":         _enum_val(o.status),
        "broker":         o.broker,
        "notes":          payload.get("notes", ""),
        "signal_side":    _enum_val(sig.side) if sig else None,
        "pnl":            _calc_order_pnl(o, holding) if with_pnl else None,
        "created_at":     str(o.created_at),
        "updated_at":     str(o.updated_at) if o.updated_at else None,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
def create_order(
    payload: OrderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_trader_or_above),
):
    """Create paper/live order with full pre-trade validation."""
    instrument = None
    if payload.instrument_id is not None:
        instrument = db.query(Instrument).filter(Instrument.id == payload.instrument_id).first()
    elif payload.symbol:
        instrument = (
            db.query(Instrument)
            .filter(Instrument.symbol == payload.symbol.upper(), Instrument.is_active == True)
            .first()
        )
    else:
        raise HTTPException(status_code=422, detail="instrument_id or symbol is required")

    if not instrument:
        ident = payload.instrument_id if payload.instrument_id is not None else payload.symbol
        raise HTTPException(status_code=404, detail=f"Instrument {ident} not found")

    # Pull target/stop from linked signal (if any)
    target_pct = stop_pct = None
    sig = None
    if payload.signal_id:
        sig = db.query(Signal).filter(Signal.id == payload.signal_id).first()
        if not sig:
            raise HTTPException(status_code=404, detail=f"Signal {payload.signal_id} not found")
        target_pct = sig.target_pct
        stop_pct   = sig.stop_pct

    # Build intent + run validation
    intent = OrderIntent(
        instrument_id=instrument.id,
        signal_id=payload.signal_id,
        side=_enum_val(payload.side),
        quantity=payload.quantity,
        price=payload.price,
        user_id=current_user.id,
        mode=_enum_val(payload.mode),
        broker=payload.broker,
        notes=payload.notes or "",
    )
    capital            = settings.DEFAULT_CAPITAL
    daily_loss_so_far  = _daily_loss_so_far(db)
    existing_positions = [] if (payload.symbol and payload.instrument_id is None) else _existing_positions_today(db, instrument.id)

    result = validate_order(
        intent=intent,
        capital=capital,
        daily_loss_so_far=daily_loss_so_far,
        existing_positions=existing_positions,
        settings={"max_daily_loss_pct": settings.MAX_DAILY_LOSS_PCT},
        target_pct=target_pct,
        stop_pct=stop_pct,
    )

    if not result.passed:
        raise HTTPException(
            status_code=400,
            detail={
                "message":        "Order blocked",
                "blocked_reason": result.blocked_reason,
                "checks":         result.checks,
            },
        )

    # Generate UID + create order — store full audit trail in raw_payload_json
    order_uid = generate_order_uid()
    raw_payload = {
        "instrument_id":         instrument.id,
        "symbol":                instrument.symbol,
        "signal_id":             payload.signal_id,
        "notes":                 payload.notes or "",
        "mode":                  _enum_val(payload.mode),
        "validation_checks":     result.checks,
        "blocked_reason":        result.blocked_reason,   # "" when validation passed
        "trade_value":           result.trade_value,
        "trade_pct_of_capital":  result.trade_pct_of_capital,
        "risk_reward_ratio":     result.risk_reward_ratio,
        "max_loss_amount":       result.max_loss_amount,
        "target_pct":            target_pct,
        "stop_pct":              stop_pct,
        "generated_at":          datetime.now(timezone.utc).isoformat(),
    }

    order = Order(
        signal_id=payload.signal_id,
        user_id=current_user.id,
        side=payload.side,
        quantity=payload.quantity,
        price=payload.price,
        mode=payload.mode,
        broker=payload.broker,
        uid=order_uid,
        status=OrderStatus.CREATED,
        raw_payload_json=raw_payload,
    )
    db.add(order)
    db.flush()   # get order.id without commit

    # Mark linked signal as executed if it was approved
    if sig and sig.status == SignalStatus.APPROVED:
        sig.status = SignalStatus.EXECUTED

    # Paper mode: instant fill + holdings update
    if payload.mode == OrderMode.PAPER:
        order.status       = OrderStatus.FILLED
        order.filled_qty   = payload.quantity
        order.filled_price = payload.price
        _update_holdings(db, instrument, _enum_val(payload.side), payload.quantity, payload.price)

    # Audit
    _audit(
        db, current_user.id,
        action="ORDER_CREATED",
        entity_type="order",
        entity_id=order.id,
        after={
            "uid":           order_uid,
            "instrument_id": instrument.id,
            "side":          _enum_val(payload.side),
            "quantity":      payload.quantity,
            "price":         payload.price,
            "mode":          _enum_val(payload.mode),
            "status":        _enum_val(order.status),
        },
    )

    db.commit()
    db.refresh(order)

    return {
        "order":      _serialize_order(order, db),
        "validation": {
            "passed":               result.passed,
            "checks":               result.checks,
            "trade_value":          result.trade_value,
            "trade_pct_of_capital": result.trade_pct_of_capital,
            "risk_reward_ratio":    result.risk_reward_ratio,
            "max_loss_amount":      result.max_loss_amount,
        },
    }


@router.get("")
def list_orders(
    mode: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    instrument_id: Optional[int] = Query(None),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD"),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    q = db.query(Order)

    if mode:
        try:
            q = q.filter(Order.mode == OrderMode(mode.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")
    if status:
        try:
            q = q.filter(Order.status == OrderStatus(status.upper()))
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    df_parsed = dt_to_parsed = None
    if date_from:
        try:
            df_parsed = datetime.strptime(date_from, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="date_from must be YYYY-MM-DD")
    if date_to:
        try:
            dt_to_parsed = datetime.strptime(date_to, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
        except ValueError:
            raise HTTPException(status_code=400, detail="date_to must be YYYY-MM-DD")
    if df_parsed and dt_to_parsed and df_parsed > dt_to_parsed:
        raise HTTPException(status_code=400, detail="date_from cannot be after date_to")
    if df_parsed:
        q = q.filter(Order.created_at >= df_parsed)
    if dt_to_parsed:
        q = q.filter(Order.created_at <= dt_to_parsed)

    orders = q.order_by(Order.created_at.desc()).limit(limit).all()

    # Filter by instrument_id post-query (no instrument_id column on orders)
    if instrument_id is not None:
        rows = []
        for o in orders:
            inst = _instrument_for_order(db, o)
            if inst and inst.id == instrument_id:
                rows.append(_serialize_order(o, db))
    else:
        rows = [_serialize_order(o, db) for o in orders]

    return {"count": len(rows), "orders": rows}


@router.get("/summary")
def orders_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Aggregate paper-trading stats: counts, total P&L, win rate, best/worst."""
    today_start = _today_start()

    all_orders = db.query(Order).all()
    paper      = [o for o in all_orders if _enum_val(o.mode) == "PAPER"]
    live       = [o for o in all_orders if _enum_val(o.mode) == "LIVE"]
    filled     = [o for o in paper if _enum_val(o.status) == "FILLED"]
    filled_today = [o for o in filled if o.created_at and o.created_at >= today_start]

    pnl_rows = []
    for o in filled:
        inst = _instrument_for_order(db, o)
        holding = (
            db.query(Holding).filter(Holding.instrument_id == inst.id).first()
            if inst else None
        )
        pnl = _calc_order_pnl(o, holding)
        if pnl is not None:
            pnl_rows.append({
                "uid":    o.uid,
                "symbol": inst.symbol if inst else None,
                "pnl":    pnl,
            })

    total_pnl   = sum(r["pnl"] for r in pnl_rows)
    win_trades  = sum(1 for r in pnl_rows if r["pnl"] > 0)
    loss_trades = sum(1 for r in pnl_rows if r["pnl"] < 0)
    decided     = win_trades + loss_trades
    win_rate    = (win_trades / decided * 100) if decided > 0 else 0.0
    avg_pnl     = (total_pnl / len(pnl_rows)) if pnl_rows else 0.0

    best  = max(pnl_rows, key=lambda r: r["pnl"]) if pnl_rows else None
    worst = min(pnl_rows, key=lambda r: r["pnl"]) if pnl_rows else None

    return {
        "total_orders":      len(all_orders),
        "paper_orders":      len(paper),
        "live_orders":       len(live),
        "filled_today":      len(filled_today),
        "total_paper_pnl":   round(total_pnl, 2),
        "win_trades":        win_trades,
        "loss_trades":       loss_trades,
        "win_rate_pct":      round(win_rate, 2),
        "avg_pnl_per_trade": round(avg_pnl, 2),
        "best_trade":        best,
        "worst_trade":       worst,
    }


@router.get("/by-instrument/{instrument_id}")
def orders_by_instrument(
    instrument_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    instrument = db.query(Instrument).filter(Instrument.id == instrument_id).first()
    if not instrument:
        raise HTTPException(status_code=404, detail=f"Instrument {instrument_id} not found")

    all_orders = db.query(Order).order_by(Order.created_at.desc()).all()
    rows = []
    for o in all_orders:
        inst = _instrument_for_order(db, o)
        if inst and inst.id == instrument_id:
            rows.append(_serialize_order(o, db))

    return {
        "instrument_id": instrument_id,
        "symbol":        instrument.symbol,
        "count":         len(rows),
        "orders":        rows,
    }


@router.get("/blotter")
def orders_blotter(
    mode: str = Query("ALL", description="PAPER | LIVE | ALL"),
    status: str = Query("all", description="filled | open | cancelled | all"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD (default: 30 days ago)"),
    date_to: Optional[str] = Query(None, description="YYYY-MM-DD (default: today)"),
    instrument_id: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Paginated order blotter with mode/status/date/instrument filters.
    Status groups: open=[CREATED, SENT, ACKNOWLEDGED, PARTIAL_FILL]; filled=[FILLED];
    cancelled=[CANCELLED, REJECTED]; all=everything.
    """
    today = date.today()
    df = today - timedelta(days=30)
    dt_to = today
    if date_from:
        try:
            df = datetime.strptime(date_from, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date_from must be YYYY-MM-DD")
    if date_to:
        try:
            dt_to = datetime.strptime(date_to, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date_to must be YYYY-MM-DD")
    # Only validate ordering when both were explicitly supplied — otherwise a
    # future-dated date_from against the today default would falsely trip 400.
    if date_from and date_to and df > dt_to:
        raise HTTPException(status_code=400, detail="date_from cannot be after date_to")
    # If only date_from was given and it's beyond the default upper bound,
    # widen dt_to so the query naturally returns 0 rather than raising.
    if df > dt_to:
        dt_to = df

    df_dt    = datetime.combine(df, datetime.min.time())
    dt_to_dt = datetime.combine(dt_to, datetime.min.time()).replace(hour=23, minute=59, second=59)

    q = db.query(Order).filter(Order.created_at >= df_dt, Order.created_at <= dt_to_dt)

    mode_norm = mode.upper()
    if mode_norm in {"PAPER", "LIVE"}:
        q = q.filter(Order.mode == OrderMode(mode_norm))
    elif mode_norm != "ALL":
        raise HTTPException(status_code=400, detail="mode must be PAPER | LIVE | ALL")

    status_norm = status.lower()
    status_groups = {
        "filled":    [OrderStatus.FILLED],
        "open":      [OrderStatus.CREATED, OrderStatus.SENT, OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIAL_FILL],
        "cancelled": [OrderStatus.CANCELLED, OrderStatus.REJECTED],
    }
    if status_norm in status_groups:
        q = q.filter(Order.status.in_(status_groups[status_norm]))
    elif status_norm != "all":
        raise HTTPException(status_code=400, detail="status must be filled | open | cancelled | all")

    # Pull all matching, then post-filter by instrument_id (no FK column on orders)
    rows = q.order_by(Order.created_at.desc()).all()
    if instrument_id is not None:
        rows = [o for o in rows if (_instrument_for_order(db, o) or None) and _instrument_for_order(db, o).id == instrument_id]

    total_count = len(rows)
    total_pages = (total_count + page_size - 1) // page_size if page_size else 0
    start = (page - 1) * page_size
    page_rows = rows[start : start + page_size]

    blotter = []
    for o in page_rows:
        ser = _serialize_order(o, db)
        # Augment with pnl_pct
        pnl = ser.get("pnl")
        notional = (o.filled_price or o.price or 0) * (o.filled_qty or o.quantity or 0)
        pnl_pct = round(pnl / notional * 100, 2) if (pnl is not None and notional > 0) else None

        blotter.append({
            "id":            ser["id"],
            "uid":           ser["uid"],
            "symbol":        ser["symbol"],
            "exchange":      ser["exchange"],
            "side":          ser["side"],
            "quantity":      ser["quantity"],
            "price":         ser["price"],
            "mode":          ser["mode"],
            "status":        ser["status"],
            "filled_qty":    ser["filled_qty"],
            "filled_price": ser["filled_price"],
            "pnl":           pnl,
            "pnl_pct":       pnl_pct,
            "broker":        ser["broker"],
            "created_at":    ser["created_at"],
            "updated_at":    ser["updated_at"],
            "signal_id":     ser["signal_id"],
            "notes":         ser["notes"],
        })

    return {
        "page":        page,
        "page_size":   page_size,
        "total_count": total_count,
        "total_pages": total_pages,
        "filters": {
            "mode":          mode_norm,
            "status":        status_norm,
            "date_from":     str(df),
            "date_to":       str(dt_to),
            "instrument_id": instrument_id,
        },
        "orders": blotter,
    }


@router.get("/pnl-summary")
def orders_pnl_summary(
    period: str = Query("today", description="today | week | month | all"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Period-based P&L stats with per-instrument breakdown sorted by pnl desc."""
    period_norm = period.lower()
    today_dt = _today_start()
    period_starts = {
        "today": today_dt,
        "week":  today_dt - timedelta(days=7),
        "month": today_dt - timedelta(days=30),
        "all":   datetime(1970, 1, 1),
    }
    if period_norm not in period_starts:
        raise HTTPException(status_code=400, detail="period must be today | week | month | all")
    start_dt = period_starts[period_norm]

    filled = (
        db.query(Order)
        .filter(Order.status == OrderStatus.FILLED, Order.created_at >= start_dt)
        .all()
    )

    rows = []                              # per-order pnl rows for this period
    by_instrument: dict[str, dict] = {}    # symbol → aggregated stats

    for o in filled:
        inst = _instrument_for_order(db, o)
        symbol = inst.symbol if inst else "UNKNOWN"
        holding = (
            db.query(Holding).filter(Holding.instrument_id == inst.id).first()
            if inst else None
        )
        pnl = _calc_order_pnl(o, holding)
        if pnl is None:
            continue
        rows.append({"symbol": symbol, "pnl": pnl})

        bucket = by_instrument.setdefault(symbol, {"trades": 0, "pnl": 0.0, "wins": 0, "losses": 0})
        bucket["trades"] += 1
        bucket["pnl"]    += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1

    realized_pnl   = sum(r["pnl"] for r in rows)
    win_count      = sum(1 for r in rows if r["pnl"] > 0)
    loss_count     = sum(1 for r in rows if r["pnl"] < 0)
    breakeven_count = sum(1 for r in rows if r["pnl"] == 0)
    decided        = win_count + loss_count
    win_rate       = (win_count / decided * 100) if decided > 0 else 0.0

    avg_win  = (sum(r["pnl"] for r in rows if r["pnl"] > 0) / win_count) if win_count else 0.0
    avg_loss = (sum(r["pnl"] for r in rows if r["pnl"] < 0) / loss_count) if loss_count else 0.0
    profit_factor = round(avg_win / abs(avg_loss), 2) if loss_count and avg_loss != 0 else None

    by_instrument_list = sorted(
        [
            {
                "symbol":    sym,
                "trades":    s["trades"],
                "pnl":       round(s["pnl"], 2),
                "win_rate":  round(s["wins"] / (s["wins"] + s["losses"]) * 100, 2)
                             if (s["wins"] + s["losses"]) > 0 else 0.0,
            }
            for sym, s in by_instrument.items()
        ],
        key=lambda r: r["pnl"],
        reverse=True,
    )

    return {
        "period":          period_norm,
        "realized_pnl":    round(realized_pnl, 2),
        "unrealized_pnl":  0.0,                    # Phase 2 — needs live prices
        "total_pnl":       round(realized_pnl, 2),
        "trades_count":    len(rows),
        "win_count":       win_count,
        "loss_count":      loss_count,
        "breakeven_count": breakeven_count,
        "win_rate_pct":    round(win_rate, 2),
        "avg_win_pnl":     round(avg_win, 2),
        "avg_loss_pnl":    round(avg_loss, 2),
        "profit_factor":   profit_factor,
        "max_drawdown":    0.0,                    # Phase 2 — needs equity curve series
        "by_instrument":   by_instrument_list,
    }


@router.get("/open-positions")
def orders_open_positions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Orders awaiting fill — typically only LIVE orders since PAPER fills instantly."""
    open_statuses = [OrderStatus.CREATED, OrderStatus.SENT]
    open_orders = (
        db.query(Order)
        .filter(Order.status.in_(open_statuses))
        .order_by(Order.created_at.desc())
        .all()
    )

    # SQLite stores created_at as naive UTC — compare against naive UTC now
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    positions = []
    for o in open_orders:
        inst = _instrument_for_order(db, o)
        created = o.created_at.replace(tzinfo=None) if o.created_at and o.created_at.tzinfo else o.created_at
        age_minutes = int((now - created).total_seconds() / 60) if created else None

        positions.append({
            "id":           o.id,
            "uid":          o.uid,
            "symbol":       inst.symbol if inst else None,
            "side":         _enum_val(o.side),
            "quantity":     o.quantity,
            "price":        o.price,
            "mode":         _enum_val(o.mode),
            "status":       _enum_val(o.status),
            "created_at":   str(o.created_at),
            "age_minutes":  age_minutes,
        })

    return {"count": len(positions), "positions": positions}


@router.post("/bulk-cancel")
def orders_bulk_cancel(
    body: BulkCancelRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    """FOUNDER — cancel many open orders at once."""
    if not body.cancel_all_open and not body.order_ids:
        raise HTTPException(
            status_code=400,
            detail="Provide order_ids: [...] or cancel_all_open: true",
        )

    open_statuses = [
        OrderStatus.CREATED, OrderStatus.SENT,
        OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIAL_FILL,
    ]

    if body.cancel_all_open:
        targets = (
            db.query(Order)
            .filter(Order.status.in_(open_statuses))
            .all()
        )
    else:
        targets = (
            db.query(Order)
            .filter(Order.id.in_(body.order_ids))
            .all()
        )

    cancelled_ids: list[int] = []
    failed: list[dict] = []

    for o in targets:
        if o.status not in open_statuses:
            failed.append({"id": o.id, "reason": f"status {_enum_val(o.status)} not cancellable"})
            continue
        o.status = OrderStatus.CANCELLED
        _audit(db, current_user.id, "ORDER_BULK_CANCELLED", "order", o.id, {"uid": o.uid})
        cancelled_ids.append(o.id)

    # Account for explicitly-requested ids that didn't match any row
    if not body.cancel_all_open and body.order_ids:
        found_ids = {o.id for o in targets}
        for oid in body.order_ids:
            if oid not in found_ids:
                failed.append({"id": oid, "reason": "order not found"})

    db.commit()

    return {
        "cancelled":     len(cancelled_ids),
        "failed":        len(failed),
        "cancelled_ids": cancelled_ids,
        "failures":      failed,
    }


@router.get("/{order_id}")
def get_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    o = db.query(Order).filter(Order.id == order_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    inst = _instrument_for_order(db, o)
    holding = (
        db.query(Holding).filter(Holding.instrument_id == inst.id).first()
        if inst else None
    )
    sig = db.query(Signal).filter(Signal.id == o.signal_id).first() if o.signal_id else None
    payload = _payload_dict(o.raw_payload_json)

    return {
        **_serialize_order(o, db),
        "instrument": (
            {
                "id":         inst.id,
                "symbol":     inst.symbol,
                "exchange":   inst.exchange,
                "asset_type": _enum_val(inst.asset_type),
                "sector":     inst.sector,
                "cap_bucket": _enum_val(inst.cap_bucket) if inst.cap_bucket else None,
            } if inst else None
        ),
        "signal": (
            {
                "id":         sig.id,
                "side":       _enum_val(sig.side),
                "confidence": sig.confidence,
                "target_pct": sig.target_pct,
                "stop_pct":   sig.stop_pct,
                "status":     _enum_val(sig.status),
            } if sig else None
        ),
        "validation_checks":    payload.get("validation_checks", {}),
        "trade_value":          payload.get("trade_value"),
        "trade_pct_of_capital": payload.get("trade_pct_of_capital"),
        "risk_reward_ratio":    payload.get("risk_reward_ratio"),
        "max_loss_amount":      payload.get("max_loss_amount"),
        "raw_payload":          payload,
    }


@router.post("/{order_id}/cancel")
def cancel_order(
    order_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_trader_or_above),
):
    o = db.query(Order).filter(Order.id == order_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")
    if o.status in [OrderStatus.FILLED, OrderStatus.CANCELLED]:
        raise HTTPException(status_code=400, detail=f"Cannot cancel order in status {_enum_val(o.status)}")

    o.status = OrderStatus.CANCELLED
    _audit(db, current_user.id, "ORDER_CANCELLED", "order", o.id, {"uid": o.uid})
    db.commit()
    return {"message": "Order cancelled", "order_id": order_id, "uid": o.uid}


@router.post("/{order_id}/notes")
def update_order_notes(
    order_id: int,
    body: NotesUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_trader_or_above),
):
    o = db.query(Order).filter(Order.id == order_id).first()
    if not o:
        raise HTTPException(status_code=404, detail="Order not found")

    payload = _payload_dict(o.raw_payload_json).copy()
    payload["notes"] = body.notes
    o.raw_payload_json = payload

    _audit(db, current_user.id, "ORDER_NOTES_UPDATED", "order", o.id, {"notes": body.notes})
    db.commit()
    db.refresh(o)
    return _serialize_order(o, db)
