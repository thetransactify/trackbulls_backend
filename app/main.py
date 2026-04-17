"""
app/main.py
FastAPI application entry point
Run: uvicorn app.main:app --reload --port 8000
"""
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger
import sys

from app.core.config import settings
from app.api.v1.router import api_router
from app.db.init_db import init_db

# ─── LOGGING ────────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stdout, level="DEBUG" if settings.DEBUG else "INFO",
           format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")
logger.add("logs/trackbulls.log", rotation="10 MB", retention="30 days", level="INFO")

# ─── APP ────────────────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.APP_NAME,
    version="1.0.0",
    description="TrackBulls AI Trading & Investment Platform — Backend API",
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# ─── CORS ───────────────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── ROUTES ─────────────────────────────────────────────────────────────────
app.include_router(api_router)


# ─── STARTUP ────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup_event():
    logger.info(f"🚀 Starting {settings.APP_NAME} in {settings.APP_ENV} mode")
    logger.info(f"📦 Database: {'SQLite (dev)' if settings.USE_SQLITE else 'PostgreSQL'}")
    logger.info(f"📝 Paper mode: {settings.PAPER_MODE}")
    init_db()
    logger.info("✅ Database ready")


@app.on_event("shutdown")
async def shutdown_event():
    logger.info("🛑 Shutting down TrackBulls API")


# ─── HEALTH ─────────────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
def health_check():
    return {
        "status": "ok",
        "app": settings.APP_NAME,
        "env": settings.APP_ENV,
        "paper_mode": settings.PAPER_MODE,
    }


@app.get("/", tags=["Root"])
def root():
    return {
        "message": "TrackBulls API is running",
        "docs": "/docs",
        "health": "/health",
        "version": "1.0.0",
    }
