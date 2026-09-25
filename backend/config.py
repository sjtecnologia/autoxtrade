from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_version: str = "0.1.0"
    app_env: str = "development"
    real_new_exposure_enabled: bool = False

    # Auth
    api_secret_token: str = Field(..., description="Bearer token para autenticação da API")

    # Database
    database_url: str = Field(..., description="URL async PostgreSQL (asyncpg)")
    database_url_sync: str = Field(..., description="URL sync PostgreSQL (psycopg2, para Alembic)")

    # Redis
    redis_url: str = "redis://redis:6379/0"
    redis_cache_ttl: int = 5

    # Celery
    celery_broker_url: str = "redis://redis:6379/1"
    celery_result_backend: str = "redis://redis:6379/2"

    # Binance
    binance_api_key: str = ""
    binance_api_secret: str = ""
    binance_paper_mode: bool = True

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # URL publica do dashboard (usada nos links das notificacoes)
    dashboard_url: str = ""

    # Trading strategy (DiDi Aguiar)
    trading_strategy_mode: str = "didi_manual"
    didi_timeframe: str = "1h"
    didi_approval_ttl_sec: int = 1800
    didi_entry_adx_min: float = 23.0
    didi_safe_break_rr: float = 1.0
    didi_rr_target: float = 2.0
    didi_exit_auto_close: bool = False

    # MT5 / DWX Connect (comunicação por arquivos)
    # Caminho para o diretório MQL5/Files do MT5
    # - MT5 na mesma máquina: caminho local ex: /Users/foo/MT5/MQL5/Files
    # - MT5 em outro host: caminho de mount SMB ex: /mnt/mt5files
    mt5_files_dir: str = ""


settings = Settings()
