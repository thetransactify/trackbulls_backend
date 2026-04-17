"""
app/core/config.py
Central configuration — reads from .env via pydantic-settings
"""
from pydantic_settings import BaseSettings
from pydantic import AnyHttpUrl
from typing import List
import os


class Settings(BaseSettings):
    # App
    APP_NAME: str = "TrackBulls API"
    APP_ENV: str = "development"
    APP_PORT: int = 8000
    DEBUG: bool = True
    SECRET_KEY: str = "changeme-replace-in-production"

    # Database
    USE_SQLITE: bool = False
    SQLITE_PATH: str = "./trackbulls_dev.db"
    DATABASE_URL: str = "sqlite:///./trackbulls_dev.db"

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CACHE_TTL: int = 10

    # JWT
    JWT_SECRET_KEY: str = "changeme-jwt-secret"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # Zerodha Kite
    KITE_API_KEY: str = ""
    KITE_API_SECRET: str = ""
    KITE_REDIRECT_URL: str = "http://localhost:8000/api/v1/broker/kite/callback"

    # Telegram
    TELEGRAM_BOT_TOKEN: str = ""
    TELEGRAM_CHAT_ID: str = ""

    # Email
    SMTP_HOST: str = "smtp.gmail.com"
    SMTP_PORT: int = 587
    SMTP_USER: str = ""
    SMTP_PASSWORD: str = ""
    REPORT_EMAIL_TO: str = ""

    # CORS
    CORS_ORIGINS: str = "http://localhost:5173,http://localhost:3000"

    # Trading defaults
    DEFAULT_CAPITAL: float = 100000.0
    MAX_DAILY_LOSS_PCT: float = 2.0
    MAX_TRADE_RISK_PCT: float = 1.0
    DEFAULT_TARGET_PCT: float = 2.0
    DEFAULT_STOPLOSS_PCT: float = 1.0
    CONFIDENCE_THRESHOLD: int = 65
    PAPER_MODE: bool = True

    def get_cors_origins(self) -> List[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",")]

    def get_database_url(self) -> str:
        if self.USE_SQLITE:
            return f"sqlite:///{self.SQLITE_PATH}"
        return self.DATABASE_URL

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"


settings = Settings()
