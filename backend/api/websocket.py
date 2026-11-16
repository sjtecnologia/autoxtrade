"""WebSocket Connection Manager com Redis pub/sub para broadcast multi-processo."""
import asyncio
import json
import logging
from typing import Any

import redis.asyncio as aioredis
from fastapi import WebSocket

from config import settings

logger = logging.getLogger(__name__)

PUBSUB_CHANNEL = "ws:broadcast"


class ConnectionManager:
    """Gerencia conexões WebSocket ativas e recebe mensagens via Redis pub/sub."""

    def __init__(self) -> None:
        self._active: list[WebSocket] = []
        self._redis: aioredis.Redis | None = None
        self._pubsub: aioredis.client.PubSub | None = None
        self._listener_task: asyncio.Task | None = None

    async def startup(self) -> None:
        """Inicializa Redis e inicia a tarefa de escuta do canal pub/sub."""
        self._redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(PUBSUB_CHANNEL)
        self._listener_task = asyncio.create_task(self._listen())
        logger.info("ConnectionManager iniciado — escutando canal '%s'", PUBSUB_CHANNEL)

    async def shutdown(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
        if self._pubsub:
            await self._pubsub.unsubscribe(PUBSUB_CHANNEL)
            await self._pubsub.aclose()
        if self._redis:
            await self._redis.aclose()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._active.append(websocket)
        logger.debug("WS conectado. Total: %d", len(self._active))

    def disconnect(self, websocket: WebSocket) -> None:
        self._active.remove(websocket)
        logger.debug("WS desconectado. Total: %d", len(self._active))

    async def broadcast(self, message: dict[str, Any]) -> None:
        """Publica mensagem no canal Redis para broadcast em todos os processos."""
        if self._redis:
            await self._redis.publish(PUBSUB_CHANNEL, json.dumps(message))

    async def _send_to_all(self, text: str) -> None:
        dead: list[WebSocket] = []
        for ws in self._active:
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        for ws in dead:
            if ws in self._active:
                self._active.remove(ws)

    async def _listen(self) -> None:
        """Escuta o canal Redis e repassa mensagens para todos os WebSockets ativos."""
        try:
            async for message in self._pubsub.listen():
                if message["type"] == "message":
                    await self._send_to_all(message["data"])
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Erro no listener pub/sub: %s", exc)


manager = ConnectionManager()
