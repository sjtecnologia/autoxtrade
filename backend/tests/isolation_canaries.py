"""Intentional failures; run explicitly to verify the isolation harness.

These are not part of normal test discovery. Each must fail in teardown even
when application code catches the transport exception.
"""
import socket
from pathlib import Path


def test_external_network_is_rejected():
    with socket.socket() as sock:
        try:
            sock.connect(("203.0.113.1", 443))
        except RuntimeError:
            pass


def test_operational_dwx_is_rejected():
    try:
        (Path.cwd() / "MQL5" / "Files" / "DWX" / "DWX_Commands_0.txt").write_text("forbidden")
    except RuntimeError:
        pass


async def test_telegram_transport_is_rejected():
    from telegram import Bot
    bot = Bot("12345:fake-test-token")
    try:
        await bot.send_message(chat_id="fake", text="forbidden")
    except RuntimeError:
        pass
