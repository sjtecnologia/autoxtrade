from celery import Celery
from celery.schedules import crontab

from config import settings

celery_app = Celery(
    "autoxtrade",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "tasks.report_tasks",
        "tasks.data_tasks",
        "tasks.ml_tasks",
        "tasks.trading_tasks",
    ],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="America/Sao_Paulo",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "daily-report": {
            "task": "tasks.report_tasks.send_daily_report",
            "schedule": crontab(hour=23, minute=0),
        },
        "equity-snapshot": {
            "task": "tasks.data_tasks.record_equity_snapshot",
            "schedule": crontab(minute=0),  # a cada hora
        },
        "drawdown-check": {
            "task": "tasks.data_tasks.check_drawdown",
            "schedule": 300.0,  # a cada 5 min
        },
        "update-market-data": {
            "task": "tasks.ml_tasks.update_market_data",
            "schedule": crontab(hour=3, minute=0),  # 03h BRT
        },
        "retrain-models": {
            "task": "tasks.ml_tasks.retrain_models",
            "schedule": crontab(day_of_week="sunday", hour=2, minute=0),
        },
        "scan-and-trade": {
            "task": "tasks.trading_tasks.scan_and_trade",
            "schedule": crontab(minute="*/15"),  # a cada 15 minutos
        },
        "didi-process-approvals": {
            "task": "tasks.trading_tasks.process_entry_approvals",
            "schedule": 30.0,  # a cada 30 segundos
        },
        "didi-monitor-open-positions": {
            "task": "tasks.trading_tasks.monitor_open_positions",
            "schedule": 60.0,  # a cada 1 minuto
        },
    },
)
