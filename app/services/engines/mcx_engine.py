"""
app/services/engines/mcx_engine.py
MCX commodity signal engine — bull/bear scoring
Based on: supply/demand, geopolitics, budget, RSI, SMA, contract positioning
"""
from dataclasses import dataclass, field
from typing import Optional


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
    )
