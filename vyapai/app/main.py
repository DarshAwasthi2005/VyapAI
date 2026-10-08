from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import routes_b2b, routes_b2c, webhooks
from app.config import settings
from app.db.session import init_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("vyapai")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.app_env == "development":
        init_db()
        logger.info("Dev DB tables ensured via create_all(). Use `alembic upgrade head` in production.")
    else:
        logger.info("Production mode: schema is managed by Alembic migrations, not create_all().")
    yield


app = FastAPI(
    title="VyapAI",
    description="Dual B2B/B2C vernacular commerce agent platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if settings.app_env == "development" else [],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(routes_b2b.router)
app.include_router(routes_b2c.router)
app.include_router(webhooks.router)


@app.get("/health")
def health():
    return {"status": "ok", "env": settings.app_env}
