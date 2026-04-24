"""
app/services/engines/mcx_engine.py
MCX commodity signal engine — bull/bear scoring
Based on: supply/demand, geopolitics, budget, RSI, SMA, contract positioning
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from app.models.models import Instrument, MarketSnapshot, MacroEvent


def get_current_mcx_session() -> tuple[str, str]:
    """
    Return (session_name, session_note) based on current IST time.
    MCX hours: Morning 09:00–17:00 IST, Evening 17:00–23:30 IST, Closed otherwise.
    """
    now_utc = datetime.now(tz=timezone.utc)
    # IST = UTC + 5:30
    ist_hour = (now_utc.hour + 5) % 24
    ist_minute = now_utc.minute
    ist_total_minutes = ist_hour * 60 + ist_minute

    morning_open  = 9 * 60       # 09:00
    evening_start = 17 * 60      # 17:00
    close_time    = 23 * 60 + 30 # 23:30

    if morning_open <= ist_total_minutes < evening_start:
        return "MORNING", "MCX morning session (09:00–17:00 IST)"
    elif evening_start <= ist_total_minutes < close_time:
        return "EVENING", "MCX evening session (17:00–23:30 IST)"
    else:
        return "CLOSED", "MCX market closed — signal for next open"


def build_mcx_input_from_db(
    instrument: "Instrument",
    snapshot: "MarketSnapshot",
    macro_events: list["MacroEvent"],
) -> "MCXInput":
    """Map DB objects to MCXInput dataclass. Derives macro impacts from MacroEvent records."""
    geo_impact = "NEUTRAL"
    budget_impact = "NEUTRAL"
    weather_impact = "NEUTRAL"

    # Sentiment priority: POSITIVE > NEGATIVE > NEUTRAL (last event of each type wins)
    for event in macro_events:
        ev_type = (event.type or "").upper()
        sentiment = (event.sentiment or "NEUTRAL").upper()
        if ev_type == "GEOPOLITICS":
            geo_impact = sentiment
        elif ev_type == "BUDGET":
            budget_impact = sentiment
        elif ev_type == "WEATHER":
            weather_impact = sentiment

    # Infer volume trend from OI change if available (fallback NEUTRAL)
    volume_trend = "NEUTRAL"
    if snapshot.volume is not None:
        if snapshot.volume > 50000:
            volume_trend = "RISING"
        elif snapshot.volume < 15000:
            volume_trend = "FALLING"

    return MCXInput(
        symbol=instrument.symbol,
        rsi=snapshot.rsi,
        sma_20=snapshot.sma_20,
        current_price=snapshot.close,
        volume_trend=volume_trend,
        geopolitical_impact=geo_impact,
        budget_impact=budget_impact,
        weather_impact=weather_impact,
    )


@dataclass
class MCXInput:
    symbol: str                             # CRUDEOIL | GOLD | SILVER
    # Technical
    rsi: Optional[float] = None             # 0–100
    sma_20: Optional[float] = None
    current_price: Optional[float] = None
    volume_trend: str = "NEUTRAL"           # RISING | FALLING | NEUTRAL
    # Fundamental
    production_vs_consumption: str = "BALANCED"  # SURPLUS | DEFICIT | BALANCED
    contract_bias_pct: Optional[float] = None    # % contracts in bull direction
    # Qualitative tags
    geopolitical_impact: str = "NEUTRAL"    # POSITIVE | NEGATIVE | NEUTRAL
    budget_impact: str = "NEUTRAL"
    weather_impact: str = "NEUTRAL"
    # Contract
    days_to_expiry: Optional[int] = None
    next_month_flow: str = "NEUTRAL"        # BUYING | SELLING | NEUTRAL (rollover signal)
    # Session
    trading_session: str = "UNKNOWN"        # MORNING | EVENING | CLOSED | UNKNOWN


@dataclass
class MCXSignalResult:
    bull_score: float = 0.0
    bear_score: float = 0.0
    signal: str = "NO_TRADE"        # BUY | SELL | NO_TRADE
    confidence: float = 0.0
    target_pct: float = 3.0
    stop_pct: float = 1.5
    reasons: list = field(default_factory=list)
    expiry_warning: bool = False
    rollover_recommended: bool = False
    session: str = "UNKNOWN"
    session_note: str = ""


def score_mcx(data: MCXInput) -> MCXSignalResult:
    """
    Generate bull/bear MCX signal based on founder rulebook.
    Bull: deficit supply, positive geopolitics/budget, RSI low, SMA above, >70% contract bull
    Bear: surplus supply, negative macro, RSI high, SMA below, >70% contract bear
    """
    bull_points = 0.0
    bear_points = 0.0
    reasons = []
    max_points = 0.0

    def add(bull: float, bear: float, reason: str, weight: float = 1.0):
        nonlocal bull_points, bear_points, max_points
        bull_points += bull * weight
        bear_points += bear * weight
        max_points += weight
        reasons.append(reason)

    # 1. Supply vs Demand (weight: 25)
    if data.production_vs_consumption == "DEFICIT":
        add(1.0, 0.0, "Supply deficit — bullish fundamental", 25)
    elif data.production_vs_consumption == "SURPLUS":
        add(0.0, 1.0, "Supply surplus — bearish fundamental", 25)
    else:
        add(0.5, 0.5, "Supply balanced — neutral", 25)

    # 2. Contract positioning (weight: 20) — 70% rule
    if data.contract_bias_pct is not None:
        if data.contract_bias_pct >= 70:
            add(1.0, 0.0, f"Contract bias {data.contract_bias_pct}% bullish (≥70%)", 20)
        elif data.contract_bias_pct <= 30:
            add(0.0, 1.0, f"Contract bias {data.contract_bias_pct}% bearish (≤30%)", 20)
        else:
            add(0.5, 0.5, f"Contract bias {data.contract_bias_pct}% — mixed", 20)

    # 3. RSI (weight: 15)
    if data.rsi is not None:
        if data.rsi < 40:
            add(1.0, 0.0, f"RSI {data.rsi} — oversold, bullish reversal likely", 15)
        elif data.rsi > 65:
            add(0.0, 1.0, f"RSI {data.rsi} — overbought, bearish reversal likely", 15)
        else:
            add(0.5, 0.5, f"RSI {data.rsi} — neutral zone", 15)

    # 4. SMA (weight: 10)
    if data.sma_20 is not None and data.current_price is not None:
        if data.current_price > data.sma_20:
            add(1.0, 0.0, f"Price {data.current_price} above SMA20 {data.sma_20} — uptrend", 10)
        else:
            add(0.0, 1.0, f"Price {data.current_price} below SMA20 {data.sma_20} — downtrend", 10)

    # 5. Volume trend (weight: 8)
    if data.volume_trend == "RISING":
        # Rising volume confirms trend direction — we'll add to whichever is leading
        add(0.6, 0.4, "Rising volume — confirms dominant trend", 8)
    elif data.volume_trend == "FALLING":
        add(0.4, 0.6, "Falling volume — momentum weakening", 8)

    # 6. Geopolitical impact (weight: 10)
    if data.geopolitical_impact == "POSITIVE":
        add(1.0, 0.0, "Geopolitical factors — bullish for commodity", 10)
    elif data.geopolitical_impact == "NEGATIVE":
        add(0.0, 1.0, "Geopolitical risk — bearish pressure", 10)

    # 7. Budget impact (weight: 7)
    if data.budget_impact == "POSITIVE":
        add(1.0, 0.0, "Budget/fiscal policy — bullish", 7)
    elif data.budget_impact == "NEGATIVE":
        add(0.0, 1.0, "Budget impact — bearish", 7)

    # 8. Weather (weight: 5)
    if data.weather_impact == "POSITIVE":
        add(1.0, 0.0, "Weather outlook — supportive", 5)
    elif data.weather_impact == "NEGATIVE":
        add(0.0, 1.0, "Adverse weather — negative impact", 5)

    # ─── Normalize scores to 0–100 ────────────────────────────────────────
    bull_score = round((bull_points / max_points) * 100, 1) if max_points > 0 else 0
    bear_score = round((bear_points / max_points) * 100, 1) if max_points > 0 else 0

    # ─── Signal decision ──────────────────────────────────────────────────
    diff = bull_score - bear_score
    if diff >= 20:
        signal = "BUY"
        confidence = min(99, 50 + diff)
    elif diff <= -20:
        signal = "SELL"
        confidence = min(99, 50 + abs(diff))
    else:
        signal = "NO_TRADE"
        confidence = 0.0

    # ─── Expiry logic ─────────────────────────────────────────────────────
    expiry_warning = False
    rollover_recommended = False
    if data.days_to_expiry is not None:
        if data.days_to_expiry <= 5:
            expiry_warning = True
            reasons.append(f"⚠️ Only {data.days_to_expiry} days to expiry — review urgently")
        if data.days_to_expiry <= 10 and signal == "SELL":
            rollover_recommended = False   # Bear: exit before expiry
            reasons.append("Bear position: exit ~10 days before expiry")
        if data.days_to_expiry <= 3 and signal == "BUY":
            rollover_recommended = True    # Bull: consider rollover
            reasons.append("Bull position near expiry — consider rolling to next month")

    session, session_note = get_current_mcx_session()
    if data.trading_session != "UNKNOWN":
        session = data.trading_session

    return MCXSignalResult(
        bull_score=bull_score,
        bear_score=bear_score,
        signal=signal,
        confidence=round(confidence, 1),
        target_pct=3.0,
        stop_pct=1.5,
        reasons=reasons,
        expiry_warning=expiry_warning,
        rollover_recommended=rollover_recommended,
        session=session,
        session_note=session_note,
    )
