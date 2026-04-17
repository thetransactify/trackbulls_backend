"""
app/services/engines/equity_engine.py
Equity scoring engine — implements founder's rulebook
Scores equities 0–100 across large/mid/small cap buckets
"""
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class FundamentalsInput:
    pe: Optional[float] = None
    roe: Optional[float] = None
    eps_growth_pct: Optional[float] = None
    sales_growth_pct: Optional[float] = None
    profit_growth_pct: Optional[float] = None
    debt_equity: Optional[float] = None
    fii_pct: Optional[float] = None
    dii_pct: Optional[float] = None
    fii_trend: str = "NEUTRAL"       # INCREASING | DECREASING | NEUTRAL
    management_score: int = 5        # 1–10 analyst rating
    macro_alignment: int = 5         # 1–10 macro fit score
    cap_bucket: str = "LARGE"        # LARGE | MID | SMALL


@dataclass
class ScoreResult:
    total_score: float = 0.0
    band: str = "WATCH"
    factors: dict = field(default_factory=dict)
    pass_count: int = 0
    total_factors: int = 0


# Thresholds per cap bucket
RULES = {
    "LARGE": {"pe_min": 10, "pe_max": 60, "roe_min": 15, "debt_max": 1.5,
              "fii_dii_min": 25, "growth_window": "5yr"},
    "MID":   {"pe_min": 10, "pe_max": 80, "roe_min": 12, "debt_max": 2.0,
              "fii_dii_min": 15, "growth_window": "3yr"},
    "SMALL": {"pe_min": 5,  "pe_max": 100, "roe_min": 10, "debt_max": 2.5,
              "fii_dii_min": 5,  "growth_window": "2yr"},
}


def score_equity(data: FundamentalsInput) -> ScoreResult:
    """
    Score a single equity instrument using founder rulebook.
    Returns ScoreResult with total 0–100, band, and per-factor breakdown.
    """
    rules = RULES.get(data.cap_bucket, RULES["LARGE"])
    factors = {}
    weighted_sum = 0.0
    total_weight = 0.0

    def add_factor(name: str, score: float, weight: float, passed: bool, reason: str):
        nonlocal weighted_sum, total_weight
        factors[name] = {"score": round(score, 1), "weight": weight,
                         "passed": passed, "reason": reason}
        weighted_sum += score * weight
        total_weight += weight

    # 1. PE ratio (weight: 15)
    if data.pe is not None:
        if rules["pe_min"] <= data.pe <= rules["pe_max"]:
            pe_score = 100 - ((data.pe - rules["pe_min"]) / (rules["pe_max"] - rules["pe_min"]) * 40)
            add_factor("pe_ratio", pe_score, 15, True, f"PE {data.pe} within {rules['pe_min']}–{rules['pe_max']}")
        elif data.pe < rules["pe_min"]:
            add_factor("pe_ratio", 60, 15, True, f"PE {data.pe} below minimum — undervalued signal")
        else:
            add_factor("pe_ratio", 10, 15, False, f"PE {data.pe} above max {rules['pe_max']} — expensive")
    else:
        add_factor("pe_ratio", 50, 15, False, "PE data missing")

    # 2. ROE (weight: 15)
    if data.roe is not None:
        if data.roe >= rules["roe_min"]:
            roe_score = min(100, 60 + (data.roe - rules["roe_min"]) * 2)
            add_factor("roe", roe_score, 15, True, f"ROE {data.roe}% ≥ {rules['roe_min']}%")
        else:
            add_factor("roe", max(0, data.roe * 3), 15, False, f"ROE {data.roe}% below min {rules['roe_min']}%")
    else:
        add_factor("roe", 50, 15, False, "ROE data missing")

    # 3. Debt/Equity (weight: 12)
    if data.debt_equity is not None:
        if data.debt_equity <= rules["debt_max"]:
            debt_score = max(40, 100 - data.debt_equity * 25)
            add_factor("debt_equity", debt_score, 12, True, f"D/E {data.debt_equity} ≤ {rules['debt_max']}")
        else:
            add_factor("debt_equity", 10, 12, False, f"D/E {data.debt_equity} exceeds max {rules['debt_max']}")
    else:
        add_factor("debt_equity", 50, 12, False, "Debt data missing")

    # 4. EPS Growth (weight: 12)
    if data.eps_growth_pct is not None:
        if data.eps_growth_pct >= 15:
            add_factor("eps_growth", min(100, 60 + data.eps_growth_pct), 12, True,
                       f"EPS growth {data.eps_growth_pct}% — strong")
        elif data.eps_growth_pct >= 0:
            add_factor("eps_growth", 40 + data.eps_growth_pct, 12, True,
                       f"EPS growth {data.eps_growth_pct}% — moderate")
        else:
            add_factor("eps_growth", 10, 12, False, f"EPS declining {data.eps_growth_pct}%")
    else:
        add_factor("eps_growth", 50, 12, False, "EPS data missing")

    # 5. Sales Growth (weight: 10)
    if data.sales_growth_pct is not None:
        score = min(100, max(0, 50 + data.sales_growth_pct * 1.5))
        add_factor("sales_growth", score, 10, data.sales_growth_pct >= 10,
                   f"Sales growth {data.sales_growth_pct}%")
    else:
        add_factor("sales_growth", 50, 10, False, "Sales data missing")

    # 6. Profit Growth (weight: 10)
    if data.profit_growth_pct is not None:
        score = min(100, max(0, 50 + data.profit_growth_pct * 1.5))
        add_factor("profit_growth", score, 10, data.profit_growth_pct >= 10,
                   f"Profit growth {data.profit_growth_pct}%")
    else:
        add_factor("profit_growth", 50, 10, False, "Profit data missing")

    # 7. FII + DII combined holding (weight: 12)
    if data.fii_pct is not None and data.dii_pct is not None:
        combined = data.fii_pct + data.dii_pct
        trend_bonus = 10 if data.fii_trend == "INCREASING" else (-10 if data.fii_trend == "DECREASING" else 0)
        score = min(100, max(0, (combined / rules["fii_dii_min"]) * 60 + trend_bonus))
        add_factor("fii_dii", score, 12,
                   combined >= rules["fii_dii_min"] and data.fii_trend != "DECREASING",
                   f"FII+DII {combined}%, trend: {data.fii_trend}")
    else:
        add_factor("fii_dii", 50, 12, False, "FII/DII data missing")

    # 8. Management quality (weight: 8)
    mgmt_score = data.management_score * 10
    add_factor("management", mgmt_score, 8, data.management_score >= 6,
               f"Management score {data.management_score}/10")

    # 9. Macro alignment (weight: 6)
    macro_score = data.macro_alignment * 10
    add_factor("macro_alignment", macro_score, 6, data.macro_alignment >= 5,
               f"Macro alignment {data.macro_alignment}/10")

    # ─── Final score ───────────────────────────────────────────────────────
    final_score = (weighted_sum / total_weight) if total_weight > 0 else 0.0
    final_score = round(min(100, max(0, final_score)), 1)

    # Determine band
    if final_score >= 80:
        band = "STRONG_BUY"
    elif final_score >= 65:
        band = "BUY"
    elif final_score >= 50:
        band = "HOLD"
    elif final_score >= 35:
        band = "WATCH"
    else:
        band = "REJECT"

    pass_count = sum(1 for f in factors.values() if f["passed"])

    return ScoreResult(
        total_score=final_score,
        band=band,
        factors=factors,
        pass_count=pass_count,
        total_factors=len(factors),
    )
