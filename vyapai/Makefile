.PHONY: up down test lint migrate revision shell logs

up:
	docker compose up --build

down:
	docker compose down -v

test:
	pytest tests/ -v --cov=app --cov-report=term-missing

lint:
	ruff check app tests

migrate:
	docker compose exec api alembic upgrade head

revision:
	docker compose exec api alembic revision --autogenerate -m "$(m)"

shell:
	docker compose exec api /bin/sh

logs:
	docker compose logs -f api
