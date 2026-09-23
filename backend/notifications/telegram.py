"""Cliente Telegram assíncrono usando python-telegram-bot."""
import logging

from telegram import Bot
from telegram.error import TelegramError

from config import settings

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self) -> None:
        self._bot: Bot | None = None

    def _get_bot(self) -> Bot:
        if self._bot is None:
            if not settings.telegram_bot_token:
                raise RuntimeError("TELEGRAM_BOT_TOKEN não configurado.")
            self._bot = Bot(token=settings.telegram_bot_token)
        return self._bot

    async def send_message(self, text: str) -> bool:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            logger.debug("Telegram não configurado — mensagem ignorada.")
            return False
        try:
            bot = self._get_bot()
            await bot.send_message(
                chat_id=settings.telegram_chat_id,
                text=text,
                parse_mode="HTML",
            )
            return True
        except TelegramError as exc:
            logger.warning("Falha ao enviar mensagem Telegram: %s", exc)
            return False

    async def send_photo(self, photo: bytes, caption: str = "") -> bool:
        if not settings.telegram_bot_token or not settings.telegram_chat_id:
            logger.debug("Telegram não configurado — imagem ignorada.")
            return False
        try:
            bot = self._get_bot()
            await bot.send_photo(
                chat_id=settings.telegram_chat_id,
                photo=photo,
                caption=caption[:1024],
                parse_mode="HTML",
            )
            return True
        except TelegramError as exc:
            logger.warning("Falha ao enviar imagem Telegram: %s", exc)
            return False

    async def send_emergency_alert(self, text: str) -> bool:
        """Envia alerta com prioridade máxima — tenta até 3 vezes na hora."""
        import asyncio
        for attempt in range(3):
            if await self.send_message(text):
                return True
            await asyncio.sleep(2**attempt)
        logger.error("Alerta de emergência não enviado após 3 tentativas.")
        return False


# Singleton
notifier = TelegramNotifier()
