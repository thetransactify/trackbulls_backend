"""
app/api/v1/router.py
Registers all endpoint routers under /api/v1
"""
from fastapi import APIRouter
from app.api.v1.endpoints import (
    auth,
    dashboard,
    portfolio,
    signals,
    orders,
    risk,
    instruments,
    reports,
    app_settings,
    scoring,
    mcx,
)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(dashboard.router)
api_router.include_router(portfolio.router)
api_router.include_router(signals.router)
api_router.include_router(orders.router)
api_router.include_router(risk.router)
api_router.include_router(instruments.router)
api_router.include_router(reports.router)
api_router.include_router(app_settings.router)
api_router.include_router(scoring.router)
api_router.include_router(mcx.router)
