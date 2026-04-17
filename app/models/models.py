"""
app/models/models.py
All SQLAlchemy ORM models for TrackBulls
Tables: users, strategies, instruments, fundamentals_snapshots,
        market_snapshots, macro_events, scores, signals,
        holdings, orders, reviews, alerts, audit_logs
"""
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime,
    Text, ForeignKey, Enum as SAEnum, JSON
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from app.db.session import Base
import enum


# ─── ENUMS ──────────────────────────────────────────────────────────────────

class UserRole(str, enum.Enum):
    FOUNDER = "FOUNDER"
    ANALYST = "ANALYST"
    TRADER = "TRADER"

class AssetType(str, enum.Enum):
    EQUITY = "EQUITY"
    MCX = "MCX"
    ETF = "ETF"
    MF = "MF"

class CapBucket(str, enum.Enum):
    LARGE = "LARGE"
    MID = "MID"
    SMALL = "SMALL"
    TRADING = "TRADING"

class SignalSide(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    NO_TRADE = "NO_TRADE"

class SignalStatus(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXECUTED = "EXECUTED"
    EXPIRED = "EXPIRED"

class OrderMode(str, enum.Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"

class OrderStatus(str, enum.Enum):
    CREATED = "CREATED"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILLED = "FILLED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"

class ScoreBand(str, enum.Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    HOLD = "HOLD"
    WATCH = "WATCH"
    REJECT = "REJECT"

class AlertSeverity(str, enum.Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


# ─── MODELS ─────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"
    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(100), nullable=False)
    email       = Column(String(150), unique=True, index=True, nullable=False)
    username    = Column(String(50), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    role        = Column(SAEnum(UserRole), default=UserRole.TRADER)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    updated_at  = Column(DateTime(timezone=True), onupdate=func.now())


class Strategy(Base):
    __tablename__ = "strategies"
    id          = Column(Integer, primary_key=True)
    name        = Column(String(100), nullable=False)
    asset_type  = Column(SAEnum(AssetType), nullable=False)
    mode        = Column(String(20), default="PAPER")   # PAPER | LIVE
    status      = Column(String(20), default="ACTIVE")  # ACTIVE | PAUSED
    config_json = Column(JSON, default={})              # thresholds, weights
    created_at  = Column(DateTime(timezone=True), server_default=func.now())
    updated_at  = Column(DateTime(timezone=True), onupdate=func.now())


class Instrument(Base):
    __tablename__ = "instruments"
    id            = Column(Integer, primary_key=True)
    symbol        = Column(String(30), nullable=False, index=True)
    exchange      = Column(String(10), nullable=False)  # NSE | BSE | MCX
    asset_type    = Column(SAEnum(AssetType), nullable=False)
    sector        = Column(String(80))
    cap_bucket    = Column(SAEnum(CapBucket))
    expiry        = Column(DateTime)                    # MCX contracts
    is_active     = Column(Boolean, default=True)
    metadata_json = Column(JSON, default={})
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    fundamentals  = relationship("FundamentalsSnapshot", back_populates="instrument")
    market_snaps  = relationship("MarketSnapshot", back_populates="instrument")
    scores        = relationship("Score", back_populates="instrument")
    signals       = relationship("Signal", back_populates="instrument")
    holdings      = relationship("Holding", back_populates="instrument")


class FundamentalsSnapshot(Base):
    __tablename__ = "fundamentals_snapshots"
    id            = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    as_of_date    = Column(DateTime, nullable=False)
    sales         = Column(Float)
    profit        = Column(Float)
    eps           = Column(Float)
    pe            = Column(Float)
    roe           = Column(Float)
    debt_equity   = Column(Float)
    fii_pct       = Column(Float)
    dii_pct       = Column(Float)
    market_cap    = Column(Float)
    extra_json    = Column(JSON, default={})
    created_at    = Column(DateTime(timezone=True), server_default=func.now())

    instrument    = relationship("Instrument", back_populates="fundamentals")


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    id            = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    ts            = Column(DateTime, nullable=False, index=True)
    open          = Column(Float)
    high          = Column(Float)
    low           = Column(Float)
    close         = Column(Float)
    volume        = Column(Float)
    oi            = Column(Float)     # open interest (MCX)
    rsi           = Column(Float)
    sma_20        = Column(Float)
    sma_50        = Column(Float)

    instrument    = relationship("Instrument", back_populates="market_snaps")


class MacroEvent(Base):
    __tablename__ = "macro_events"
    id            = Column(Integer, primary_key=True)
    type          = Column(String(30))   # BUDGET | WEATHER | GEOPOLITICS | NEWS
    title         = Column(String(200), nullable=False)
    sentiment     = Column(String(10))   # POSITIVE | NEGATIVE | NEUTRAL
    effective_from = Column(DateTime)
    tags_json     = Column(JSON, default={})
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


class Score(Base):
    __tablename__ = "scores"
    id            = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    strategy_id   = Column(Integer, ForeignKey("strategies.id"))
    ts            = Column(DateTime(timezone=True), server_default=func.now())
    score_value   = Column(Float, nullable=False)   # 0–100
    band          = Column(SAEnum(ScoreBand))
    factors_json  = Column(JSON, default={})        # per-factor breakdown

    instrument    = relationship("Instrument", back_populates="scores")


class Signal(Base):
    __tablename__ = "signals"
    id            = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    strategy_id   = Column(Integer, ForeignKey("strategies.id"))
    ts            = Column(DateTime(timezone=True), server_default=func.now())
    side          = Column(SAEnum(SignalSide), nullable=False)
    target_pct    = Column(Float)
    stop_pct      = Column(Float)
    confidence    = Column(Float)
    status        = Column(SAEnum(SignalStatus), default=SignalStatus.PENDING)
    reasons_json  = Column(JSON, default={})
    review_date   = Column(DateTime)
    approved_by   = Column(Integer, ForeignKey("users.id"))

    instrument    = relationship("Instrument", back_populates="signals")
    orders        = relationship("Order", back_populates="signal")


class Holding(Base):
    __tablename__ = "holdings"
    id            = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    quantity      = Column(Float, nullable=False)
    avg_cost      = Column(Float, nullable=False)
    asset_bucket  = Column(SAEnum(CapBucket))
    thesis_status = Column(String(20), default="ACTIVE")
    last_review   = Column(DateTime)
    notes         = Column(Text)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())
    updated_at    = Column(DateTime(timezone=True), onupdate=func.now())

    instrument    = relationship("Instrument", back_populates="holdings")


class Order(Base):
    __tablename__ = "orders"
    id              = Column(Integer, primary_key=True)
    signal_id       = Column(Integer, ForeignKey("signals.id"))
    user_id         = Column(Integer, ForeignKey("users.id"))
    side            = Column(SAEnum(SignalSide), nullable=False)
    quantity        = Column(Float, nullable=False)
    price           = Column(Float)
    broker          = Column(String(20), default="ZERODHA")
    mode            = Column(SAEnum(OrderMode), default=OrderMode.PAPER)
    uid             = Column(String(50), unique=True, index=True)  # internal UID
    broker_order_id = Column(String(100))
    status          = Column(SAEnum(OrderStatus), default=OrderStatus.CREATED)
    filled_qty      = Column(Float, default=0)
    filled_price    = Column(Float)
    raw_payload_json = Column(JSON, default={})
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    updated_at      = Column(DateTime(timezone=True), onupdate=func.now())

    signal          = relationship("Signal", back_populates="orders")


class Review(Base):
    __tablename__ = "reviews"
    id            = Column(Integer, primary_key=True)
    instrument_id = Column(Integer, ForeignKey("instruments.id"), nullable=False)
    review_type   = Column(String(30))   # QUARTERLY | BIMONTHLY | MONTHLY | SWING | MCX
    due_date      = Column(DateTime, nullable=False)
    completed_at  = Column(DateTime)
    outcome       = Column(String(20))   # HOLD | EXIT | INCREASE | REDUCE
    notes         = Column(Text)
    created_at    = Column(DateTime(timezone=True), server_default=func.now())


class Alert(Base):
    __tablename__ = "alerts"
    id                  = Column(Integer, primary_key=True)
    severity            = Column(SAEnum(AlertSeverity), default=AlertSeverity.INFO)
    category            = Column(String(40))  # RISK | SIGNAL | REVIEW | SYSTEM
    message             = Column(Text, nullable=False)
    related_entity_type = Column(String(30))
    related_entity_id   = Column(Integer)
    acknowledged_at     = Column(DateTime)
    created_at          = Column(DateTime(timezone=True), server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id            = Column(Integer, primary_key=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"))
    action        = Column(String(60), nullable=False)
    entity_type   = Column(String(40))
    entity_id     = Column(Integer)
    before_json   = Column(JSON)
    after_json    = Column(JSON)
    ts            = Column(DateTime(timezone=True), server_default=func.now())


class Settings(Base):
    __tablename__ = "app_settings"
    id            = Column(Integer, primary_key=True)
    key           = Column(String(100), unique=True, nullable=False)
    value         = Column(Text)
    updated_by    = Column(Integer, ForeignKey("users.id"))
    updated_at    = Column(DateTime(timezone=True), onupdate=func.now())
