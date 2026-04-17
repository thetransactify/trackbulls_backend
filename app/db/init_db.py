"""
app/db/init_db.py
Creates all tables and seeds default admin user
Run: python -m app.db.init_db
"""
from app.db.session import engine, SessionLocal, Base
from app.models.models import User, Strategy, UserRole, AssetType
from app.core.security import hash_password
from loguru import logger


def create_tables():
    logger.info("Creating database tables...")
    Base.metadata.create_all(bind=engine)
    logger.info("✅ Tables created")


def seed_default_data():
    db = SessionLocal()
    try:
        # Default admin user
        existing = db.query(User).filter(User.username == "admin").first()
        if not existing:
            admin = User(
                name="Founder",
                email="admin@trackbulls.com",
                username="admin",
                hashed_password=hash_password("admin@123"),
                role=UserRole.FOUNDER,
                is_active=True,
            )
            db.add(admin)
            logger.info("✅ Default admin user created (username: admin, password: admin@123)")

        # Default strategies
        strategies = [
            {"name": "Equity Large Cap", "asset_type": AssetType.EQUITY,
             "config_json": {"cap_bucket": "LARGE", "pe_max": 60, "roe_min": 15,
                             "debt_max": 1.5, "review_days": 90}},
            {"name": "Equity Mid Cap",   "asset_type": AssetType.EQUITY,
             "config_json": {"cap_bucket": "MID", "pe_max": 80, "roe_min": 12,
                             "debt_max": 2.0, "review_days": 60}},
            {"name": "Equity Small Cap", "asset_type": AssetType.EQUITY,
             "config_json": {"cap_bucket": "SMALL", "pe_max": 100, "roe_min": 10,
                             "debt_max": 2.5, "review_days": 30}},
            {"name": "Equity Swing Trading", "asset_type": AssetType.EQUITY,
             "config_json": {"hold_max_days": 20, "target_pct": 2.0,
                             "stop_pct": 1.0, "review_days": 5}},
            {"name": "MCX Commodity",    "asset_type": AssetType.MCX,
             "config_json": {"target_pct": 3.0, "stop_pct": 1.5,
                             "review_days": 5, "expiry_exit_days_bear": 10}},
        ]
        for s in strategies:
            exists = db.query(Strategy).filter(Strategy.name == s["name"]).first()
            if not exists:
                db.add(Strategy(**s))

        db.commit()
        logger.info("✅ Default strategies seeded")
    except Exception as e:
        db.rollback()
        logger.error(f"Seed error: {e}")
    finally:
        db.close()


def init_db():
    create_tables()
    seed_default_data()


if __name__ == "__main__":
    init_db()
