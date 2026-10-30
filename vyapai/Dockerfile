FROM python:3.12-slim AS base

# psycopg2-binary ships its own libpq, so only the runtime library and
# curl (for the HEALTHCHECK) are needed. Deliberately NO gcc/libpq-dev: a
# compiler toolchain quadruples build time and image size for zero benefit.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY requirements.txt .
# Generous timeout + retries: a slow/flaky build network must not fail the
# first deploy (Render's builders are fast, but this also helps local runs).
RUN pip install --no-cache-dir --timeout 120 --retries 5 -r requirements.txt

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
