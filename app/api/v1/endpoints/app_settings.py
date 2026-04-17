"""
app/api/v1/endpoints/app_settings.py
GET  /settings           — get all app settings
POST /settings           — upsert a setting key/value
POST /settings/broker    — save broker API credentials
GET  /settings/strategies — list strategies
POST /settings/strategies/{id} — update strategy thresholds
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
from app.db.session import get_db
from app.core.deps import get_current_user, require_founder
from app.models.models import Settings as AppSettings, Strategy, User

router = APIRouter(prefix="/settings", tags=["Settings"])


class SettingUpsert(BaseModel):
    key: str
    value: str


class BrokerConfig(BaseModel):
    broker: str = "ZERODHA"
    api_key: str
    api_secret: str


class StrategyUpdate(BaseModel):
    config_json: dict
    mode: Optional[str] = None   # PAPER | LIVE


@router.get("")
def get_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = db.query(AppSettings).all()
    return {
        "settings": {r.key: r.value for r in rows}
    }


@router.post("")
def upsert_setting(
    payload: SettingUpsert,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    row = db.query(AppSettings).filter(AppSettings.key == payload.key).first()
    if row:
        row.value = payload.value
        row.updated_by = current_user.id
    else:
        row = AppSettings(key=payload.key, value=payload.value, updated_by=current_user.id)
        db.add(row)
    db.commit()
    return {"message": f"Setting '{payload.key}' saved"}


@router.post("/broker")
def save_broker_config(
    payload: BrokerConfig,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    # Store masked — never return secret in plain text
    for key, val in [
        (f"broker_{payload.broker.lower()}_api_key", payload.api_key),
        (f"broker_{payload.broker.lower()}_api_secret", payload.api_secret),
    ]:
        row = db.query(AppSettings).filter(AppSettings.key == key).first()
        if row:
            row.value = val
        else:
            db.add(AppSettings(key=key, value=val, updated_by=current_user.id))
    db.commit()
    return {"message": f"{payload.broker} credentials saved"}


@router.get("/strategies")
def list_strategies(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    strategies = db.query(Strategy).all()
    return {
        "strategies": [
            {"id": s.id, "name": s.name, "asset_type": s.asset_type,
             "mode": s.mode, "status": s.status, "config": s.config_json}
            for s in strategies
        ]
    }


@router.post("/strategies/{strategy_id}")
def update_strategy(
    strategy_id: int,
    payload: StrategyUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    strategy.config_json = payload.config_json
    if payload.mode:
        strategy.mode = payload.mode.upper()
    db.commit()
    return {"message": "Strategy updated", "id": strategy_id}
