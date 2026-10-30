FROM python:3.12-slim AS base

# psycopg2-binary needs libpq at runtime; build-essential only needed transiently
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq-dev gcc curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY frontend ./frontend
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

# Run as non-root
RUN useradd --create-home vyapai && chown -R vyapai:vyapai /srv
USER vyapai

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
