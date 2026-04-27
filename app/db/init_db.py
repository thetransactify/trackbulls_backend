"""
app/db/init_db.py
Creates all tables and seeds default admin user
Run: python -m app.db.init_db
"""
from app.db.session import engine, SessionLocal, Base
from app.models.models import User, Strategy, UserRole, AssetType, Settings as AppSettings
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

        # Default app settings
        default_settings = {
            "default_capital": "100000",
            "max_daily_loss_pct": "2",
            "max_trade_size_pct": "20",
            "ai_confidence_threshold": "65",
            "paper_mode": "true",
            "zerodha_api_key": "",
            "zerodha_api_secret": "",
            "telegram_enabled": "false",
            "telegram_chat_id": "",
        }
        for key, value in default_settings.items():
            exists = db.query(AppSettings).filter(AppSettings.key == key).first()
            if not exists:
                db.add(AppSettings(key=key, value=value))

        # Default strategies
        strategies = [
            {"name": "Equity Investment", "asset_type": AssetType.EQUITY,
             "config_json": {"large_cap_allocation_pct": 40, "mid_cap_allocation_pct": 30,
                             "small_cap_allocation_pct": 20, "trading_bucket_pct": 10,
                             "min_roe": 12, "max_debt_equity": 2, "max_pe_large": 60,
                             "max_pe_mid": 80, "max_pe_small": 100, "min_fii_dii_pct": 0}},
            {"name": "Equity Trading", "asset_type": AssetType.EQUITY,
             "config_json": {"daily_target_pct": 2, "stop_loss_pct": 1,
                             "min_confidence": 65, "max_holding_days": 20,
                             "volume_spike_multiplier": 1.5}},
            {"name": "MCX Trading", "asset_type": AssetType.MCX,
             "config_json": {"mcx_target_pct": 3, "mcx_stop_loss_pct": 1.5,
                             "min_bull_score": 65, "min_bear_score": 65,
                             "expiry_warning_days": 5,
                             "contract_bias_threshold_pct": 2}},
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
