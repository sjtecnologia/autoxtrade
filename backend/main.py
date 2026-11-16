import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from api.router import api_router
from api.websocket import manager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    logger.info("Iniciando autoxtrade API...")
    await manager.startup()
    yield
    logger.info("Encerrando autoxtrade API...")
    await manager.shutdown()


app = FastAPI(
    title="autoxtrade",
    version="0.1.0",
    description="Robot de trading automatizado — API",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Rotas REST
app.include_router(api_router)

# Rota de health no root (atalho conveniente)
from api.routes.health import router as health_router  # noqa: E402
app.include_router(health_router)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    await manager.connect(websocket)
    try:
        while True:
            # Mantém a conexão aberta; mensagens chegam via Redis pub/sub
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
