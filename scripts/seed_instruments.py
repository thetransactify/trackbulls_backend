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
from app.models.models import Instrument, FundamentalsSnapshot, Holding, AssetType, CapBucket

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


def run():
    db = SessionLocal()
    try:
        print("\n── Seeding instruments ──────────────────────────────────")
        symbol_id = seed_instruments(db)

        print("\n── Seeding TCS fundamentals ─────────────────────────────")
        seed_tcs_fundamentals(db, symbol_id["TCS"])

        print("\n── Seeding sample holding ───────────────────────────────")
        seed_sample_holding(db, symbol_id["TCS"])

        print("\n✅ Seed complete.\n")
    finally:
        db.close()


if __name__ == "__main__":
    run()
