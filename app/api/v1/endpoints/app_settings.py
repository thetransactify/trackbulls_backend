"""
app/api/v1/endpoints/app_settings.py
Settings, broker credentials, and strategy configuration endpoints.
"""
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import get_current_user, require_founder
from app.db.session import get_db
from app.models.models import Settings as AppSettings, Strategy, User

router = APIRouter(prefix="/settings", tags=["Settings"])


DEFAULT_SETTINGS: dict[str, Any] = {
    "default_capital": 100000,
    "max_daily_loss_pct": 2,
    "max_trade_size_pct": 20,
    "ai_confidence_threshold": 65,
    "paper_mode": True,
    "zerodha_api_key": "",
    "zerodha_api_secret": "",
    "telegram_enabled": False,
    "telegram_chat_id": "",
}

BOOL_KEYS = {"paper_mode", "telegram_enabled"}
FLOAT_KEYS = {
    "default_capital",
    "max_daily_loss_pct",
    "max_trade_size_pct",
    "ai_confidence_threshold",
}
PERCENT_KEYS = {
    "max_daily_loss_pct",
    "max_trade_size_pct",
    "ai_confidence_threshold",
}
SENSITIVE_KEYS = {"zerodha_api_secret"}
MASKED_VALUE = "********"


def _parse_bool(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise HTTPException(status_code=400, detail=f"{key} must be boolean")


def _parse_float(value: Any, key: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail=f"{key} must be numeric")

    if key == "default_capital" and parsed <= 0:
        raise HTTPException(status_code=400, detail="default_capital must be positive")
    if key in PERCENT_KEYS and not 0 <= parsed <= 100:
        raise HTTPException(status_code=400, detail=f"{key} must be between 0 and 100")
    return parsed


def _stored_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _typed_setting(key: str, value: Any) -> Any:
    if key in BOOL_KEYS:
        return _parse_bool(value, key)
    if key in FLOAT_KEYS:
        parsed = _parse_float(value, key)
        return int(parsed) if parsed.is_integer() else parsed
    return "" if value is None else str(value)


def _masked(value: Any) -> str:
    return MASKED_VALUE if value else ""


def _normalize_update_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "settings" in payload and isinstance(payload["settings"], dict):
        return payload["settings"]
    if "key" in payload and "value" in payload:
        return {payload["key"]: payload["value"]}
    return payload


def _validate_setting(key: str, value: Any) -> Any:
    if key not in DEFAULT_SETTINGS:
        raise HTTPException(status_code=400, detail=f"Unsupported setting: {key}")
    if key in BOOL_KEYS:
        return _parse_bool(value, key)
    if key in FLOAT_KEYS:
        return _parse_float(value, key)
    if key in SENSITIVE_KEYS and value == MASKED_VALUE:
        return value
    return "" if value is None else str(value)


def _upsert_setting(db: Session, key: str, value: Any, user_id: int) -> None:
    row = db.query(AppSettings).filter(AppSettings.key == key).first()
    if row:
        row.value = _stored_value(value)
        row.updated_by = user_id
    else:
        db.add(AppSettings(key=key, value=_stored_value(value), updated_by=user_id))


def _settings_response(db: Session) -> dict[str, Any]:
    rows = {row.key: row.value for row in db.query(AppSettings).all()}
    settings: dict[str, Any] = {}
    for key, default_value in DEFAULT_SETTINGS.items():
        raw_value = rows.get(key, default_value)
        if key in SENSITIVE_KEYS:
            settings[key] = _masked(raw_value)
        else:
            settings[key] = _typed_setting(key, raw_value)

    # Backward compatibility for credentials saved by the previous Module 9 API.
    legacy_key = rows.get("broker_zerodha_api_key")
    legacy_secret = rows.get("broker_zerodha_api_secret")
    if legacy_key and not settings["zerodha_api_key"]:
        settings["zerodha_api_key"] = legacy_key
    if legacy_secret and not settings["zerodha_api_secret"]:
        settings["zerodha_api_secret"] = _masked(legacy_secret)
    return settings


def _has_broker_credentials(db: Session) -> bool:
    rows = {row.key: row.value for row in db.query(AppSettings).all()}
    api_key = rows.get("zerodha_api_key") or rows.get("broker_zerodha_api_key")
    api_secret = rows.get("zerodha_api_secret") or rows.get("broker_zerodha_api_secret")
    return bool(api_key and api_secret)


def _validate_strategy_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise HTTPException(status_code=400, detail="config_json must be an object")

    for key, value in config.items():
        if isinstance(value, bool) or value is None or isinstance(value, str):
            continue
        if not isinstance(value, (int, float)):
            raise HTTPException(status_code=400, detail=f"{key} must be a scalar value")
        lower_key = key.lower()
        is_threshold = (
            lower_key.endswith("_pct")
            or "confidence" in lower_key
            or lower_key.endswith("_score")
        )
        if is_threshold and not 0 <= float(value) <= 100:
            raise HTTPException(status_code=400, detail=f"{key} must be between 0 and 100")
        if value < 0:
            raise HTTPException(status_code=400, detail=f"{key} must be zero or greater")
    return config


@router.get("")
def get_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return {"settings": _settings_response(db)}


@router.post("")
def update_settings(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    updates = _normalize_update_payload(payload)
    if not updates:
        raise HTTPException(status_code=400, detail="No settings provided")

    saved_keys: list[str] = []
    for key, value in updates.items():
        parsed_value = _validate_setting(key, value)
        if key in SENSITIVE_KEYS and parsed_value == MASKED_VALUE:
            continue
        _upsert_setting(db, key, parsed_value, current_user.id)
        saved_keys.append(key)

    db.commit()
    return {"message": "Settings saved", "settings": _settings_response(db), "saved_keys": saved_keys}


@router.post("/broker")
def save_broker_config(
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    api_key = payload.get("api_key") or payload.get("zerodha_api_key")
    api_secret = payload.get("api_secret") or payload.get("zerodha_api_secret")

    if api_key is None and api_secret is None:
        raise HTTPException(status_code=400, detail="Broker API key or secret is required")

    if api_key is not None and api_key != MASKED_VALUE:
        _upsert_setting(db, "zerodha_api_key", str(api_key), current_user.id)
    if api_secret is not None and api_secret != MASKED_VALUE:
        _upsert_setting(db, "zerodha_api_secret", str(api_secret), current_user.id)

    db.commit()
    return {"message": "ZERODHA credentials saved", "settings": _settings_response(db)}


@router.post("/broker/test")
def test_broker_connection(
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    if not _has_broker_credentials(db):
        raise HTTPException(status_code=400, detail="Broker credentials missing")
    return {
        "status": "ok",
        "message": "Broker credentials found. Live validation pending.",
    }


@router.get("/strategies")
def list_strategies(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    strategies = db.query(Strategy).order_by(Strategy.id.asc()).all()
    return {
        "strategies": [
            {
                "id": strategy.id,
                "name": strategy.name,
                "asset_type": strategy.asset_type.value if strategy.asset_type else None,
                "mode": strategy.mode,
                "status": strategy.status,
                "config": strategy.config_json or {},
                "config_json": strategy.config_json or {},
            }
            for strategy in strategies
        ]
    }


@router.post("/strategies/{strategy_id}")
def update_strategy(
    strategy_id: int,
    payload: dict[str, Any],
    db: Session = Depends(get_db),
    current_user: User = Depends(require_founder),
):
    strategy = db.query(Strategy).filter(Strategy.id == strategy_id).first()
    if not strategy:
        raise HTTPException(status_code=404, detail="Strategy not found")

    config = payload.get("config_json") or payload.get("config") or payload
    strategy.config_json = _validate_strategy_config(config)

    mode = payload.get("mode")
    if mode:
        normalized_mode = str(mode).upper()
        if normalized_mode not in {"PAPER", "LIVE"}:
            raise HTTPException(status_code=400, detail="mode must be PAPER or LIVE")
        strategy.mode = normalized_mode

    db.commit()
    return {
        "message": "Strategy updated",
        "id": strategy_id,
        "strategy": {
            "id": strategy.id,
            "name": strategy.name,
            "asset_type": strategy.asset_type.value if strategy.asset_type else None,
            "mode": strategy.mode,
            "status": strategy.status,
            "config": strategy.config_json or {},
            "config_json": strategy.config_json or {},
        },
    }
