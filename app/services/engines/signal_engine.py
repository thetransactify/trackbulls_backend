"""
app/services/engines/signal_engine.py
Pure function signal generation for equity swing trading.
No DB calls — accepts pre-fetched data, returns a SignalOutput.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional


# ── Input / Output dataclasses ────────────────────────────────────────────────

@dataclass
class SignalInput:
    instrument_id: int
    symbol: str
    score: float
    band: str                               # STRONG_BUY | BUY | HOLD | WATCH | REJECT
    rsi: Optional[float] = None             # 0–100
    sma_20: Optional[float] = None
    sma_50: Optional[float] = None
    current_price: Optional[float] = None
    volume_trend: str = "NEUTRAL"           # RISING | FALLING | NEUTRAL
    momentum: str = "NEUTRAL"              # STRONG_UP | UP | NEUTRAL | DOWN | STRONG_DOWN
    news_sentiment: str = "NEUTRAL"        # POSITIVE | NEGATIVE | NEUTRAL
    macro_sentiment: str = "NEUTRAL"       # POSITIVE | NEGATIVE | NEUTRAL
    cap_bucket: str = "LARGE"              # LARGE | MID | SMALL
    strategy_config: dict = field(default_factory=dict)


@dataclass
class SignalOutput:
    side: str           # BUY | SELL | HOLD | NO_TRADE
    confidence: float   # 0–100
    target_pct: float
    stop_pct: float
    reasons: list
    invalidation: str
    hold_days: int
    review_date: datetime
    strength: str       # STRONG | MODERATE | WEAK


# ── Cap bucket helpers ────────────────────────────────────────────────────────

_TARGETS = {
    "LARGE": (2.0, 1.0),
    "MID":   (3.0, 1.5),
    "SMALL": (4.0, 2.0),
}

_HOLD_DAYS = {
    "LARGE": 10,
    "MID":   15,
    "SMALL": 20,
}


# ── Main engine ───────────────────────────────────────────────────────────────

def generate_equity_signal(data: SignalInput) -> SignalOutput:
    cap = data.cap_bucket.upper() if data.cap_bucket else "LARGE"
    today = datetime.now(tz=timezone.utc)

    # ── Bull technical conditions ─────────────────────────────────────────────
    bull_conditions: list[tuple[bool, str]] = [
        (
            data.momentum in ("STRONG_UP", "UP"),
            f"Momentum is {data.momentum}",
        ),
        (
            data.volume_trend == "RISING",
            "Volume is rising (buying pressure confirmed)",
        ),
        (
            data.rsi is not None and data.rsi < 60,
            f"RSI {data.rsi:.1f} — not overbought, room to run" if data.rsi is not None else "",
        ),
        (
            data.current_price is not None
            and data.sma_20 is not None
            and data.current_price > data.sma_20,
            f"Price ₹{data.current_price} above SMA20 ₹{data.sma_20:.2f}" if (
                data.current_price is not None and data.sma_20 is not None
            ) else "",
        ),
    ]

    # ── Bear technical conditions ─────────────────────────────────────────────
    bear_conditions: list[tuple[bool, str]] = [
        (
            data.momentum in ("DOWN", "STRONG_DOWN"),
            f"Momentum is {data.momentum}",
        ),
        (
            data.volume_trend == "RISING",
            "Rising volume confirms downward pressure",
        ),
        (
            data.rsi is not None and data.rsi > 65,
            f"RSI {data.rsi:.1f} — overbought, reversal risk" if data.rsi is not None else "",
        ),
        (
            data.current_price is not None
            and data.sma_20 is not None
            and data.current_price < data.sma_20,
            f"Price ₹{data.current_price} below SMA20 ₹{data.sma_20:.2f}" if (
                data.current_price is not None and data.sma_20 is not None
            ) else "",
        ),
    ]

    bull_met = [(ok, reason) for ok, reason in bull_conditions if ok]
    bear_met = [(ok, reason) for ok, reason in bear_conditions if ok]

    is_bull = (
        data.score >= 65
        and len(bull_met) >= 2
        and data.news_sentiment != "NEGATIVE"
        and data.macro_sentiment != "NEGATIVE"
    )
    is_bear = (
        data.score < 40
        and len(bear_met) >= 2
        and data.news_sentiment != "POSITIVE"
    )
    is_hold = not is_bull and not is_bear and 50 <= data.score < 65

    # ── Determine side ────────────────────────────────────────────────────────
    if is_bull:
        side = "BUY"
    elif is_bear:
        side = "SELL"
    elif is_hold:
        side = "HOLD"
    else:
        side = "NO_TRADE"

    # ── Confidence ────────────────────────────────────────────────────────────
    if data.score >= 80:
        base_confidence = 75.0
    elif data.score >= 65:
        base_confidence = 60.0
    elif data.score >= 50:
        base_confidence = 45.0
    else:
        base_confidence = 30.0

    extra = 0.0
    if side == "BUY":
        # Each additional bull condition beyond the required 2 adds +5 (cap +20)
        extra += min(len(bull_met) * 5, 20)
        if data.news_sentiment == "POSITIVE":
            extra += 5
        if data.macro_sentiment == "POSITIVE":
            extra += 5
    elif side == "SELL":
        extra += min(len(bear_met) * 5, 20)
        if data.news_sentiment == "NEGATIVE":
            extra += 5
        if data.macro_sentiment == "NEGATIVE":
            extra += 5

    confidence = min(base_confidence + extra, 95.0)

    # ── Target / stop ─────────────────────────────────────────────────────────
    default_target, default_stop = _TARGETS.get(cap, (2.0, 1.0))
    cfg = data.strategy_config or {}
    target_pct = float(cfg.get("target_pct", default_target))
    stop_pct   = float(cfg.get("stop_pct",  default_stop))

    # ── Hold days ─────────────────────────────────────────────────────────────
    hold_days = int(cfg.get("hold_days", _HOLD_DAYS.get(cap, 10)))

    # ── Reasons ───────────────────────────────────────────────────────────────
    reasons: list[str] = []
    reasons.append(f"Score {data.score:.1f} — band: {data.band}")

    if side == "BUY":
        reasons.append(f"Fundamental quality confirmed ({data.band})")
        reasons.extend(reason for _, reason in bull_met if reason)
        if data.news_sentiment == "POSITIVE":
            reasons.append("Positive news sentiment supports upside")
        if data.macro_sentiment == "POSITIVE":
            reasons.append("Macro environment favorable")
        if data.news_sentiment == "NEUTRAL":
            reasons.append("No negative news headwinds")
        if data.macro_sentiment == "NEUTRAL":
            reasons.append("Macro backdrop is neutral")

    elif side == "SELL":
        reasons.append(f"Weak fundamentals detected ({data.band})")
        reasons.extend(reason for _, reason in bear_met if reason)
        if data.news_sentiment == "NEGATIVE":
            reasons.append("Negative news sentiment adds downside pressure")
        if data.macro_sentiment == "NEGATIVE":
            reasons.append("Macro headwinds confirmed")

    elif side == "HOLD":
        reasons.append(f"Score in HOLD range (50–64) — no clear directional edge")
        if data.momentum not in ("STRONG_UP", "UP", "DOWN", "STRONG_DOWN"):
            reasons.append("Momentum is neutral — wait for clearer direction")

    else:
        reasons.append("Conditions insufficient for a directional trade")
        if data.score < 65 and data.score >= 40:
            reasons.append(f"Score {data.score:.1f} is in HOLD zone but technical setup is unclear")
        if data.news_sentiment == "NEGATIVE":
            reasons.append("Negative news prevents a BUY signal")
        if data.macro_sentiment == "NEGATIVE":
            reasons.append("Macro headwinds prevent a BUY signal")

    # ── Invalidation ─────────────────────────────────────────────────────────
    if side == "BUY":
        sl_price = (
            f"₹{data.current_price * (1 - stop_pct / 100):.2f}"
            if data.current_price else f"{stop_pct}% below entry"
        )
        invalidation = (
            f"Signal invalid if price drops below stop loss ({sl_price}) "
            "or RSI crosses 70"
        )
    elif side == "SELL":
        invalidation = (
            "Signal invalid if price recovers above SMA20 "
            "or a positive news catalyst emerges"
        )
    elif side == "HOLD":
        invalidation = "Re-evaluate on next review date"
    else:
        invalidation = "Re-score instrument after fresh fundamentals or market data"

    # ── Strength ─────────────────────────────────────────────────────────────
    if confidence >= 75:
        strength = "STRONG"
    elif confidence >= 55:
        strength = "MODERATE"
    else:
        strength = "WEAK"

    return SignalOutput(
        side=side,
        confidence=round(confidence, 1),
        target_pct=target_pct,
        stop_pct=stop_pct,
        reasons=reasons,
        invalidation=invalidation,
        hold_days=hold_days,
        review_date=today + timedelta(days=hold_days),
        strength=strength,
    )
