"""Isolation is installed before test collection (including import-time effects)."""
import fcntl
import ipaddress
import os
from pathlib import Path
import socket
import sys
import tempfile
from contextvars import ContextVar

import pytest

_ROOT = Path(tempfile.mkdtemp(prefix="autoxtrade-tests-" )).resolve()
tempfile.tempdir = str(_ROOT)
_violations = []
_socketpair_setup = ContextVar("socketpair_setup", default=False)
_original_socketpair = socket.socketpair


def _local_socketpair(*args, **kwargs):
    token = _socketpair_setup.set(True)
    try:
        return _original_socketpair(*args, **kwargs)
    finally:
        _socketpair_setup.reset(token)


socket.socketpair = _local_socketpair


def _deny(message):
    _violations.append(message)
    raise RuntimeError(message)


def _resolve_audit_path(args):
    path = Path(os.fsdecode(args[0]))
    if path.is_absolute():
        return path.resolve()
    if len(args) > 1 and isinstance(args[1], int) and args[1] >= 0:
        if hasattr(fcntl, "F_GETPATH"):
            try:
                fd_path = fcntl.fcntl(args[1], fcntl.F_GETPATH, b"\0" * 1024).split(b"\0", 1)[0]
                return (Path(os.fsdecode(fd_path)) / path).resolve()
            except OSError:
                pass
        for fd_root in ("/dev/fd", "/proc/self/fd"):
            try:
                return (Path(os.readlink(f"{fd_root}/{args[1]}")) / path).resolve()
            except OSError:
                pass
    return path.resolve()


def _audit(event, args):
    if event in {"socket.connect", "socket.sendto"}:
        address = args[-1]
        # No implicit permission for localhost databases/Redis either.
        allowed = os.environ.get("AUTOXTRADE_TEST_ENDPOINTS", "").split(",")
        if isinstance(address, tuple):
            host, port = address[:2]
            try:
                local = ipaddress.ip_address(host).is_loopback
            except ValueError:
                local = False
            if local and _socketpair_setup.get():
                return  # asyncio's private Windows wakeup pair, not a service
            if local and f"{host}:{port}" in allowed:
                return
        _deny("Test attempted network access outside explicit local test endpoints")
    if event == "socket.getaddrinfo":
        host = args[0]
        if host not in ("localhost", "127.0.0.1", "::1", None):
            _deny("Test attempted external DNS")
    path_events = {"open", "os.listdir", "os.scandir", "os.mkdir", "os.remove", "os.rmdir", "os.rename"}
    if event in path_events and isinstance(args[0], (str, bytes, os.PathLike)):
        path = _resolve_audit_path(args)
        if path.name == ".env" or path.name.startswith(".env.") and path.name != ".env.example":
            _deny("Test attempted to read environment secrets")
        operational = any(p.lower() in {"dwx", "mql5"} for p in path.parts) or path.name.startswith("DWX_")
        if operational and not path.is_relative_to(_ROOT):
            _deny("Test attempted operational DWX access")
        if event == "open" and path.suffix.lower() in {".pkl", ".pickle", ".joblib"} and not path.is_relative_to(_ROOT):
            _deny("Test attempted untrusted model access")
    if event in {"subprocess.Popen", "os.system"}:
        # CCXT's bundled toolz probes its version using this read-only command.
        if event == "subprocess.Popen" and (
            args[1] in (
                "git rev-parse --git-dir", "git.cmd rev-parse --git-dir", "git.exe rev-parse --git-dir",
                ["git", "rev-parse", "--git-dir"], ["git.cmd", "rev-parse", "--git-dir"],
            )
            or (
                isinstance(args[1], (list, tuple))
                and len(args[1]) >= 2
                and args[1][0] in ("git", "git.cmd", "git.exe")
                and args[1][1] == "describe"
            )
        ):
            return
        _deny("Test attempted a subprocess outside the isolation boundary")


sys.addaudithook(_audit)

# Do not let process environment or .env provide credentials to collected modules.
import pydantic_settings
pydantic_settings.BaseSettings.model_config["env_file"] = None
os.environ.update({
    "API_SECRET_TOKEN": "test-only-token",
    "DATABASE_URL": "postgresql+asyncpg://test:test@127.0.0.1:15432/autoxtrade_test",
    "DATABASE_URL_SYNC": "postgresql://test:test@127.0.0.1:15432/autoxtrade_test",
    "REDIS_URL": "redis://127.0.0.1:16379/15",
    "CELERY_BROKER_URL": "memory://", "CELERY_RESULT_BACKEND": "cache+memory://",
    "BINANCE_API_KEY": "", "BINANCE_API_SECRET": "", "BINANCE_PAPER_MODE": "true",
    "TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "",
    "MT5_FILES_DIR": str(_ROOT / "mt5"), "REAL_NEW_EXPOSURE_ENABLED": "false",
})
# Patch the dotenv source itself: Settings overrides BaseSettings.model_config.
from pydantic_settings.sources import DotEnvSettingsSource
DotEnvSettingsSource._read_env_files = lambda self: {}


def pytest_configure(config):
    config.option.basetemp = str(_ROOT / "pytest")


@pytest.fixture(autouse=True)
def enforce_isolation(monkeypatch):
    previous_count = len(_violations)
    from telegram import Bot
    async def forbidden(*args, **kwargs):
        _deny("Test attempted Telegram transport")
    monkeypatch.setattr(Bot, "_post", forbidden)
    yield
    assert len(_violations) == previous_count, "Isolation violations (even if swallowed): " + repr(_violations[previous_count:])


def pytest_sessionfinish(session, exitstatus):
    if _violations:
        session.exitstatus = 1
