"""
app/api/v1/endpoints/orders.py
POST /orders        — create paper/live order
GET  /orders        — filterable order blotter
GET  /orders/{id}   — order detail
POST /orders/{id}/cancel
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import uuid
from app.db.session import get_db
from app.core.deps import get_current_user, require_trader_or_above
from app.models.models import Order, Signal, User, OrderMode, OrderStatus, SignalStatus, SignalSide

router = APIRouter(prefix="/orders", tags=["Orders"])


class OrderCreate(BaseModel):
    signal_id: Optional[int] = None
    instrument_id: int
    side: SignalSide
    quantity: float
    price: Optional[float] = None
    mode: OrderMode = OrderMode.PAPER
    broker: str = "ZERODHA"


@router.post("", status_code=201)
def create_order(
    payload: OrderCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_trader_or_above),
):
    # TODO: run pre-trade risk check here
    order_uid = f"TB-{uuid.uuid4().hex[:10].upper()}"

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
    )
    db.add(order)

    # If linked to a signal, mark it as executed
    if payload.signal_id:
        sig = db.query(Signal).filter(Signal.id == payload.signal_id).first()
        if sig and sig.status == SignalStatus.APPROVED:
            sig.status = SignalStatus.EXECUTED

    db.commit()
    db.refresh(order)

    if payload.mode == OrderMode.PAPER:
        # Paper mode — instantly mark filled
        order.status = OrderStatus.FILLED
        order.filled_qty = order.quantity
        order.filled_price = order.price or 0.0
        db.commit()

    # TODO: Phase 2 — send to Zerodha Kite for LIVE mode

    return {"id": order.id, "uid": order_uid, "status": order.status, "mode": order.mode}


@router.get("")
def list_orders(
    mode: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(50, le=200),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Order).order_by(Order.created_at.desc())
    if mode:
        query = query.filter(Order.mode == mode.upper())
    if status:
        query = query.filter(Order.status == status.upper())

    orders = query.limit(limit).all()
    return {
        "count": len(orders),
        "orders": [
            {"id": o.id, "uid": o.uid, "side": o.side, "quantity": o.quantity,
             "price": o.price, "mode": o.mode, "status": o.status,
             "filled_qty": o.filled_qty, "filled_price": o.filled_price,
             "broker": o.broker, "created_at": str(o.created_at)}
            for o in orders
        ],
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
    return {
        "id": o.id, "uid": o.uid, "side": o.side, "quantity": o.quantity,
        "price": o.price, "mode": o.mode, "status": o.status,
        "filled_qty": o.filled_qty, "filled_price": o.filled_price,
        "broker": o.broker, "raw_payload": o.raw_payload_json,
        "created_at": str(o.created_at), "updated_at": str(o.updated_at),
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
        raise HTTPException(status_code=400, detail=f"Cannot cancel order in status {o.status}")

    o.status = OrderStatus.CANCELLED
    db.commit()
    return {"message": "Order cancelled", "order_id": order_id}
