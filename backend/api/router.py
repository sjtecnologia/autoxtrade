from fastapi import APIRouter

from api.routes.approvals import router as approvals_router
from api.routes.config import router as config_router
from api.routes.health import router as health_router
from api.routes.market import router as market_router
from api.routes.positions import router as positions_router
from api.routes.ml import router as ml_router
from api.routes.risk import router as risk_router
from api.routes.trades import router as trades_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health_router)
api_router.include_router(approvals_router)
api_router.include_router(config_router)
api_router.include_router(market_router)
api_router.include_router(positions_router)
api_router.include_router(risk_router)
api_router.include_router(ml_router)
api_router.include_router(trades_router)
