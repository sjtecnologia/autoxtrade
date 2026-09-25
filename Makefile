# autoxtrade — Makefile para desenvolvimento local (sem Docker)
# Requer: Python 3.11+, Node 20+, PostgreSQL 16, Redis 7 (via Homebrew)

VENV := backend/.venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip
UV   := $(VENV)/bin/uvicorn
AL   := $(VENV)/bin/alembic
CEL  := $(VENV)/bin/celery

.PHONY: help setup install migrate seed api worker beat frontend dev logs

help:
	@echo "Comandos disponíveis:"
	@echo "  make setup      — cria venv, instala deps Python e Node"
	@echo "  make install    — instala apenas deps Python"
	@echo "  make migrate    — roda alembic upgrade head"
	@echo "  make seed       — insere BotConfig padrão no banco"
	@echo "  make api        — sobe FastAPI com hot-reload"
	@echo "  make worker     — sobe Celery worker"
	@echo "  make beat       — sobe Celery beat"
	@echo "  make frontend   — sobe Next.js dev"
	@echo "  make dev        — sobe api + frontend em paralelo"

setup: install
	cd frontend && npm install

install:
	python3 -m venv $(VENV)
	$(PIP) install --quiet -r backend/requirements.txt

migrate:
	cd backend && $(AL) upgrade head

seed:
	cd backend && $(PY) scripts/seed_db.py

api:
	cd backend && $(UV) main:app --host 0.0.0.0 --port 8000 --reload

worker:
	cd backend && PYTHONPATH=$(CURDIR)/backend $(CEL) -A tasks.celery_app:celery_app worker --loglevel=info --concurrency=2 --pool=solo

beat:
	cd backend && PYTHONPATH=$(CURDIR)/backend $(CEL) -A tasks.celery_app:celery_app beat --loglevel=info

frontend:
	cd frontend && npm run dev

dev:
	@echo "Subindo API e Frontend em paralelo... (Ctrl+C para parar)"
	@$(MAKE) api & $(MAKE) frontend & wait
