"""
scripts/seed_instruments.py
Seed sample instruments, TCS fundamentals, and one sample holding.
Run: python scripts/seed_instruments.py
"""
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.models.models import Instrument, FundamentalsSnapshot, MarketSnapshot, MacroEvent, Holding, AssetType, CapBucket

INSTRUMENTS = [
    # symbol       exchange  asset_type        sector            cap_bucket
    ("TCS",        "NSE",    AssetType.EQUITY, "IT",             CapBucket.LARGE),
    ("INFY",       "NSE",    AssetType.EQUITY, "IT",             CapBucket.LARGE),
    ("RELIANCE",   "NSE",    AssetType.EQUITY, "Energy",         CapBucket.LARGE),
    ("HDFCBANK",   "NSE",    AssetType.EQUITY, "Banking",        CapBucket.LARGE),
    ("WIPRO",      "NSE",    AssetType.EQUITY, "IT",             CapBucket.MID),
    ("TATAMOTORS", "NSE",    AssetType.EQUITY, "Auto",           CapBucket.MID),
    ("ADANIENT",   "NSE",    AssetType.EQUITY, "Infrastructure", CapBucket.MID),
    ("IRCTC",      "NSE",    AssetType.EQUITY, "Travel",         CapBucket.SMALL),
    ("ZOMATO",     "NSE",    AssetType.EQUITY, "Food Tech",      CapBucket.SMALL),
    ("CRUDEOIL",   "MCX",    AssetType.MCX,    "Commodity",      None),
    ("GOLD",       "MCX",    AssetType.MCX,    "Commodity",      None),
    ("SILVER",     "MCX",    AssetType.MCX,    "Commodity",      None),
]


def seed_instruments(db) -> dict[str, int]:
    """Insert instruments that don't already exist. Returns {symbol: id} map."""
    symbol_id: dict[str, int] = {}
    for symbol, exchange, asset_type, sector, cap_bucket in INSTRUMENTS:
        existing = db.query(Instrument).filter(Instrument.symbol == symbol).first()
        if existing:
            print(f"  skip  {symbol} (already exists, id={existing.id})")
            symbol_id[symbol] = existing.id
        else:
            instr = Instrument(
                symbol=symbol,
                exchange=exchange,
                asset_type=asset_type,
                sector=sector,
                cap_bucket=cap_bucket,
                is_active=True,
            )
            db.add(instr)
            db.flush()  # populate instr.id before commit
            symbol_id[symbol] = instr.id
            print(f"  added {symbol} ({exchange}, {asset_type.value}, {sector})")

    db.commit()
    return symbol_id


def seed_tcs_fundamentals(db, tcs_id: int) -> None:
    """Add one fundamentals snapshot for TCS if none exists for today."""
    today = datetime.now(tz=timezone.utc).date()
    existing = (
        db.query(FundamentalsSnapshot)
        .filter(
            FundamentalsSnapshot.instrument_id == tcs_id,
            FundamentalsSnapshot.as_of_date >= datetime(today.year, today.month, today.day),
        )
        .first()
    )
    if existing:
        print(f"  skip  TCS fundamentals (already exists for today)")
        return

    snap = FundamentalsSnapshot(
        instrument_id=tcs_id,
        as_of_date=datetime.now(tz=timezone.utc),
        pe=28.5,
        roe=45.2,
        eps=None,
        debt_equity=0.1,
        fii_pct=18.5,
        dii_pct=12.0,
        extra_json={"eps_growth": 12.5, "sales_growth": 15.0},
    )
    db.add(snap)
    db.commit()
    print("  added TCS fundamentals (pe=28.5, roe=45.2, eps_growth=12.5, sales_growth=15.0)")


FUNDAMENTALS_DATA = [
    # symbol        pe     roe    eps    sales       profit    debt_eq  fii    dii    mktcap    eps_g  sales_g  profit_g
    ("INFY",       26.2,  32.1,  62.5,  153000.0,  26040.0,  0.05,   15.2,   9.8,  620000.0,  12.5,   8.5,    10.2),
    ("RELIANCE",   24.8,   9.2,  98.3,  877000.0,  67000.0,  0.44,   22.1,  14.3, 1950000.0,   8.2,  12.4,    15.3),
    ("HDFCBANK",   18.5,  17.8,  82.4,  200000.0,  60000.0,  7.20,   28.4,  16.2, 1200000.0,  18.5,  20.1,    22.4),
    ("WIPRO",      22.1,  18.4,  22.8,   90000.0,  11500.0,  0.18,    6.2,   8.4,  240000.0,   6.2,   4.8,     5.9),
    ("TATAMOTORS",  8.2,  22.6,  45.2,  440000.0,  21000.0,  1.82,   18.6,  12.1,  310000.0,  32.1,  24.6,    45.8),
    ("ADANIENT",   62.4,  12.8,  38.6,  240000.0,  10200.0,  2.18,    4.2,   9.6,  280000.0,  15.4,  28.2,    18.9),
    ("IRCTC",      52.4,  38.2,  18.6,    4200.0,   1100.0,  0.02,    3.8,  11.2,   82000.0,  22.1,  18.5,    24.3),
    ("ZOMATO",    280.0,   4.2,   1.8,   14200.0,    380.0,  0.08,   12.4,   8.6,  185000.0, 120.0,  68.2,   200.0),
]


def seed_all_fundamentals(db, symbol_id: dict[str, int]) -> None:
    """Add fundamentals for all equity instruments. Skip if already exists for today."""
    today = datetime.now(tz=timezone.utc).date()
    today_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    for row in FUNDAMENTALS_DATA:
        (symbol, pe, roe, eps, sales, profit, debt_eq,
         fii, dii, mktcap, eps_g, sales_g, profit_g) = row

        inst_id = symbol_id.get(symbol)
        if not inst_id:
            print(f"  skip  {symbol} fundamentals (instrument not found)")
            continue

        existing = (
            db.query(FundamentalsSnapshot)
            .filter(
                FundamentalsSnapshot.instrument_id == inst_id,
                FundamentalsSnapshot.as_of_date >= today_dt,
            )
            .first()
        )
        if existing:
            print(f"  skip  {symbol} fundamentals (already exists for today)")
            continue

        snap = FundamentalsSnapshot(
            instrument_id=inst_id,
            as_of_date=datetime.now(tz=timezone.utc),
            pe=pe,
            roe=roe,
            eps=eps,
            sales=sales,
            profit=profit,
            debt_equity=debt_eq,
            fii_pct=fii,
            dii_pct=dii,
            market_cap=mktcap,
            extra_json={
                "eps_growth":    eps_g,
                "sales_growth":  sales_g,
                "profit_growth": profit_g,
            },
        )
        db.add(snap)
        print(f"  added {symbol} fundamentals (pe={pe}, roe={roe}, market_cap={mktcap:.0f})")

    db.commit()


MARKET_SNAPSHOT_DATA = [
    # symbol       close    rsi   sma_20   sma_50    volume
    ("TCS",       3920.0,  58.2,  3850.0,  3780.0,  1250000),
    ("INFY",      1542.0,  52.4,  1510.0,  1480.0,  2100000),
    ("RELIANCE",  2890.0,  61.2,  2820.0,  2750.0,  3400000),
    ("HDFCBANK",  1685.0,  48.6,  1650.0,  1620.0,  4200000),
    ("WIPRO",      298.0,  44.8,   292.0,   285.0,  1800000),
    ("TATAMOTORS", 945.0,  55.4,   920.0,   895.0,  5600000),
    ("ADANIENT",  2840.0,  68.2,  2780.0,  2650.0,  2100000),
    ("IRCTC",      785.0,  42.6,   768.0,   745.0,   980000),
    ("ZOMATO",     224.0,  72.4,   218.0,   205.0,  8900000),
]


def seed_market_snapshots(db, symbol_id: dict[str, int]) -> None:
    """Add one MarketSnapshot per instrument for today. Skip if one already exists."""
    today = datetime.now(tz=timezone.utc).date()
    today_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    for symbol, close, rsi, sma_20, sma_50, volume in MARKET_SNAPSHOT_DATA:
        inst_id = symbol_id.get(symbol)
        if not inst_id:
            print(f"  skip  {symbol} market snapshot (instrument not found)")
            continue

        existing = (
            db.query(MarketSnapshot)
            .filter(
                MarketSnapshot.instrument_id == inst_id,
                MarketSnapshot.ts >= today_dt,
            )
            .first()
        )
        if existing:
            print(f"  skip  {symbol} market snapshot (already exists for today)")
            continue

        snap = MarketSnapshot(
            instrument_id=inst_id,
            ts=datetime.now(tz=timezone.utc),
            open=round(close * 0.990, 2),
            high=round(close * 1.015, 2),
            low=round(close * 0.985, 2),
            close=close,
            volume=float(volume),
            rsi=rsi,
            sma_20=sma_20,
            sma_50=sma_50,
        )
        db.add(snap)
        print(f"  added {symbol} market snapshot (close={close}, rsi={rsi}, sma_20={sma_20})")

    db.commit()


def seed_sample_holding(db, tcs_id: int) -> None:
    """Add one TCS holding only if no holdings exist yet."""
    if db.query(Holding).first():
        print("  skip  sample holding (holdings already exist)")
        return

    holding = Holding(
        instrument_id=tcs_id,
        quantity=10,
        avg_cost=3850.0,
        asset_bucket=CapBucket.LARGE,
        thesis_status="ACTIVE",
        notes="Seed holding — strong large-cap IT compounder",
    )
    db.add(holding)
    db.commit()
    print("  added holding: TCS × 10 @ ₹3850 (LARGE, invested=₹38,500)")


MCX_SNAPSHOT_DATA = [
    # symbol      close     open      high      low      rsi   sma_20   sma_50   volume   oi
    ("CRUDEOIL",  6420.0,  6380.0,  6465.0,  6355.0,  44.2,  6280.0,  6150.0,  85000,  120000),
    ("GOLD",     72400.0, 72100.0, 72650.0, 71950.0,  56.8, 71200.0, 70100.0,  18000,   32000),
    ("SILVER",   88500.0, 88000.0, 89200.0, 87800.0,  62.4, 86400.0, 84200.0,  22000,   28000),
]

MCX_MACRO_EVENTS = [
    # commodity   type            title                                                 sentiment
    ("CRUDEOIL", "GEOPOLITICS",  "Middle East supply disruption risk elevated",         "POSITIVE"),
    ("CRUDEOIL", "BUDGET",       "India crude import duty unchanged in budget",         "NEUTRAL"),
    ("GOLD",     "GEOPOLITICS",  "Global uncertainty drives safe haven demand",         "POSITIVE"),
    ("GOLD",     "BUDGET",       "Gold import duty reduced by 5% in budget",            "POSITIVE"),
    ("SILVER",   "GEOPOLITICS",  "Industrial demand from EV sector rising",             "POSITIVE"),
    ("SILVER",   "WEATHER",      "Mining output steady, no weather disruption",         "NEUTRAL"),
]


def seed_mcx_market_data(db, symbol_id: dict[str, int]) -> None:
    """Add MCX market snapshots and macro events. Skip if already exist for today."""
    today = datetime.now(tz=timezone.utc).date()
    today_dt = datetime(today.year, today.month, today.day, tzinfo=timezone.utc)

    # ── Market snapshots ──────────────────────────────────────────────────────
    for symbol, close, open_, high, low, rsi, sma_20, sma_50, volume, oi in MCX_SNAPSHOT_DATA:
        inst_id = symbol_id.get(symbol)
        if not inst_id:
            print(f"  skip  {symbol} MCX snapshot (instrument not found)")
            continue

        existing = (
            db.query(MarketSnapshot)
            .filter(
                MarketSnapshot.instrument_id == inst_id,
                MarketSnapshot.ts >= today_dt,
            )
            .first()
        )
        if existing:
            print(f"  skip  {symbol} MCX snapshot (already exists for today)")
            continue

        snap = MarketSnapshot(
            instrument_id=inst_id,
            ts=datetime.now(tz=timezone.utc),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=float(volume),
            oi=float(oi),
            rsi=rsi,
            sma_20=sma_20,
            sma_50=sma_50,
        )
        db.add(snap)
        print(f"  added {symbol} MCX snapshot (close={close}, rsi={rsi}, oi={oi})")

    db.commit()

    # ── Macro events ──────────────────────────────────────────────────────────
    for commodity, ev_type, title, sentiment in MCX_MACRO_EVENTS:
        existing = db.query(MacroEvent).filter(MacroEvent.title == title).first()
        if existing:
            print(f"  skip  macro event: \"{title[:50]}…\" (already exists)")
            continue

        event = MacroEvent(
            type=ev_type,
            title=title,
            sentiment=sentiment,
            effective_from=datetime.now(tz=timezone.utc),
            tags_json={"commodity": commodity},
        )
        db.add(event)
        print(f"  added macro event [{ev_type}] {sentiment}: {title[:60]}")

    db.commit()


def run():
    db = SessionLocal()
    try:
        print("\n── Seeding instruments ──────────────────────────────────")
        symbol_id = seed_instruments(db)

        print("\n── Seeding TCS fundamentals ─────────────────────────────")
        seed_tcs_fundamentals(db, symbol_id["TCS"])

        print("\n── Seeding all equity fundamentals ──────────────────────")
        seed_all_fundamentals(db, symbol_id)

        print("\n── Seeding market snapshots ─────────────────────────────")
        seed_market_snapshots(db, symbol_id)

        print("\n── Seeding MCX market data + macro events ───────────────")
        seed_mcx_market_data(db, symbol_id)

        print("\n── Seeding sample holding ───────────────────────────────")
        seed_sample_holding(db, symbol_id["TCS"])

        print("\n✅ Seed complete.\n")
    finally:
        db.close()


if __name__ == "__main__":
    run()
