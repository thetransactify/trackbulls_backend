"""
app/services/engines/order_engine.py
Paper trading order management engine — pure logic, no DB calls.

Validates order intents against capital, daily-loss, duplicate, and risk-reward
rules; generates order UIDs; computes per-order P&L on exit.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
import secrets


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class OrderIntent:
    instrument_id: int
    side: str                              # BUY | SELL
    quantity: float
    price: float
    user_id: int
    signal_id: Optional[int] = None
    mode: str = "PAPER"
    broker: str = "ZERODHA"
    strategy_id: Optional[int] = None
    notes: str = ""


@dataclass
class OrderValidationResult:
    passed: bool
    checks: dict = field(default_factory=dict)
    blocked_reason: str = ""
    trade_value: float = 0.0
    trade_pct_of_capital: float = 0.0
    risk_reward_ratio: float = 0.0
    max_loss_amount: float = 0.0


# ── Validation ────────────────────────────────────────────────────────────────

# Reasons in plain English keyed by check name (first failing check wins).
_BLOCK_REASONS = {
    "quantity_valid":          "Quantity must be greater than zero",
    "price_valid":             "Price must be greater than zero",
    "capital_sufficient":      "Trade value exceeds 20% of available capital",
    "daily_loss_ok":           "Daily loss limit reached — no more orders allowed today",
    "position_not_duplicate":  "Duplicate position — same instrument and side already filled today",
    "risk_reward_acceptable":  "Risk-reward ratio below 1.5 — signal not favorable enough",
}


def validate_order(
    intent: OrderIntent,
    capital: float,
    daily_loss_so_far: float,
    existing_positions: list,
    settings: dict,
    target_pct: Optional[float] = None,
    stop_pct: Optional[float] = None,
) -> OrderValidationResult:
    """
    Validate a paper-trading order intent against all pre-trade rules.

    `existing_positions` is a list of dicts/objects representing today's filled
    orders, each having `instrument_id`, `side`, and `status` (FILLED expected).
    `target_pct`/`stop_pct` come from the linked signal — if absent, the order
    is treated as a manual order and risk-reward check is skipped.
    """
    checks: dict[str, bool] = {}

    # a. quantity_valid
    checks["quantity_valid"] = (
        intent.quantity is not None and intent.quantity > 0
    )

    # b. price_valid
    checks["price_valid"] = (
        intent.price is not None and intent.price > 0
    )

    # Compute trade value (used by remaining checks)
    trade_value = (intent.quantity or 0) * (intent.price or 0)
    trade_pct_of_capital = (trade_value / capital * 100) if capital > 0 else 0.0

    # c. capital_sufficient — single trade ≤ 20% of capital
    checks["capital_sufficient"] = (
        capital > 0 and trade_value <= capital * 0.20
    )

    # d. daily_loss_ok — accumulated losses below configured limit
    max_daily_loss_pct = float(settings.get("max_daily_loss_pct", 2.0))
    daily_loss_limit = capital * max_daily_loss_pct / 100.0
    checks["daily_loss_ok"] = daily_loss_so_far < daily_loss_limit

    # e. position_not_duplicate — no FILLED order for same (instrument, side) today
    duplicate = False
    for pos in existing_positions or []:
        pos_instrument = _attr(pos, "instrument_id")
        pos_side       = _attr(pos, "side")
        pos_status     = _attr(pos, "status")
        if (
            pos_instrument == intent.instrument_id
            and _norm(pos_side) == _norm(intent.side)
            and _norm(pos_status) == "FILLED"
        ):
            duplicate = True
            break
    checks["position_not_duplicate"] = not duplicate

    # f. risk_reward_acceptable — only enforced when a signal is linked
    if intent.signal_id is not None and target_pct and stop_pct and stop_pct > 0:
        risk_reward_ratio = target_pct / stop_pct
        checks["risk_reward_acceptable"] = risk_reward_ratio >= 1.5
    else:
        risk_reward_ratio = (target_pct / stop_pct) if (target_pct and stop_pct) else 0.0
        checks["risk_reward_acceptable"] = True   # manual order — skip

    # max loss = trade value × stop % (fallback 1% if stop unknown)
    if stop_pct and stop_pct > 0:
        max_loss_amount = trade_value * stop_pct / 100.0
    else:
        max_loss_amount = trade_value * 0.01

    # First failing check name → human reason
    blocked_reason = ""
    for name in (
        "quantity_valid", "price_valid", "capital_sufficient",
        "daily_loss_ok", "position_not_duplicate", "risk_reward_acceptable",
    ):
        if not checks.get(name, False):
            blocked_reason = _BLOCK_REASONS[name]
            break

    return OrderValidationResult(
        passed=all(checks.values()),
        checks=checks,
        blocked_reason=blocked_reason,
        trade_value=round(trade_value, 2),
        trade_pct_of_capital=round(trade_pct_of_capital, 2),
        risk_reward_ratio=round(risk_reward_ratio, 2),
        max_loss_amount=round(max_loss_amount, 2),
    )


# ── UID generator ─────────────────────────────────────────────────────────────

def generate_order_uid() -> str:
    """Format: TB-YYYYMMDD-XXXXXX (6 random uppercase hex chars)."""
    today = datetime.now(tz=timezone.utc).strftime("%Y%m%d")
    suffix = secrets.token_hex(3).upper()   # 3 bytes → 6 hex chars
    return f"TB-{today}-{suffix}"


# ── P&L calculator ────────────────────────────────────────────────────────────

def calculate_order_pnl(
    side: str,
    quantity: float,
    entry_price: float,
    exit_price: float,
) -> dict:
    """
    Per-order realized P&L on exit.
    BUY:  gross = (exit - entry) * qty
    SELL: gross = (entry - exit) * qty   (short)
    """
    side_norm = _norm(side)
    if side_norm == "BUY":
        gross_pnl = (exit_price - entry_price) * quantity
    elif side_norm == "SELL":
        gross_pnl = (entry_price - exit_price) * quantity
    else:
        raise ValueError(f"Unsupported side '{side}' — expected BUY or SELL")

    notional = entry_price * quantity
    pnl_pct = (gross_pnl / notional * 100) if notional > 0 else 0.0

    if gross_pnl > 0:
        result = "PROFIT"
    elif gross_pnl < 0:
        result = "LOSS"
    else:
        result = "BREAKEVEN"

    return {
        "gross_pnl": round(gross_pnl, 2),
        "pnl_pct":   round(pnl_pct, 2),
        "result":    result,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _attr(obj, name: str):
    """Read attribute from object or key from dict — works for ORM rows or dicts."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _norm(value) -> str:
    """Normalize enum/string values to upper-case string for comparison."""
    if value is None:
        return ""
    if hasattr(value, "value"):
        value = value.value
    return str(value).upper()
